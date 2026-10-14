
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Genera una playlist M3U enriquecida y una guía XMLTV filtrada."""

import argparse
import csv
import gzip
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
GUIDES_URL = "https://iptv-org.github.io/api/guides.json"
FEEDS_URL = "https://iptv-org.github.io/api/feeds.json"
LOGOS_URL = "https://iptv-org.github.io/api/logos.json"
COUNTRIES_URL = "https://iptv-org.github.io/api/countries.json"
LANGUAGES_URL = "https://iptv-org.github.io/api/languages.json"
TIMEZONES_URL = "https://iptv-org.github.io/api/timezones.json"
USER_AGENT = "m3u-epg-auto/2.0"
TIMEOUT = 45
MAX_DOWNLOAD = 35 * 1024 * 1024


def download(url):
    """Descarga una fuente con un límite de tamaño."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        data = response.read(MAX_DOWNLOAD + 1)
        if len(data) > MAX_DOWNLOAD:
            raise ValueError("La descarga supera el límite de 35 MB.")

        if (
            response.headers.get("Content-Encoding", "").lower() == "gzip"
            or data[:2] == b"\x1f\x8b"
        ):
            data = gzip.decompress(data)

        return data


def get_json(url):
    data = json.loads(download(url).decode("utf-8-sig"))
    if not isinstance(data, list):
        raise ValueError("El catálogo descargado no es una lista.")
    return data


def norm(value):
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.replace("&", " y ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def names_for(channel):
    names = [channel.get("name", "")]
    aliases = channel.get("alt_names") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    names.extend(aliases)
    return [n.strip() for n in names if isinstance(n, str) and n.strip()]


def names_for_feed(feed):
    names = [feed.get("name", "")]
    aliases = feed.get("alt_names") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    names.extend(aliases)
    return [n.strip() for n in names if isinstance(n, str) and n.strip()]


def match_channel(name, index):
    """Solo acepta nombres exactos normalizados y una única identidad de canal."""
    key = norm(name)
    if not key:
        return None
    candidates = index.get(key, [])
    unique = {str(c.get("id")): c for c in candidates if c.get("id")}
    if len(unique) == 1:
        return next(iter(unique.values()))
    return None


def best_logo(channel_id, feed_id, logos):
    """Devuelve un logo HTTPS de la identidad/feed exactos; evita logos ambiguos."""
    candidates = [
        item for item in logos
        if item.get("channel") == channel_id
        and item.get("in_use") is True
        and str(item.get("url") or "").startswith("https://")
    ]
    if feed_id:
        feed_matches = [item for item in candidates if item.get("feed") == feed_id]
        if feed_matches:
            candidates = feed_matches
        else:
            candidates = [item for item in candidates if not item.get("feed")]
    else:
        candidates = [item for item in candidates if not item.get("feed")]
    candidates.sort(key=lambda item: (
        0 if str(item.get("format") or "").upper() in {"PNG", "JPEG", "WEBP"} else 1,
        -(int(item.get("width") or 0) * int(item.get("height") or 0)),
    ))
    return str(candidates[0].get("url")) if candidates else ""


def split_extinf(line):
    content = line[len("#EXTINF:"):]
    quoted = False
    for i, char in enumerate(content):
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            return content[:i], content[i + 1:].strip()
    return content, ""


ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


def attributes(header):
    return {m.group(1): m.group(2) for m in ATTR_RE.finditer(header)}


def set_attr(header, key, value):
    pattern = re.compile(r'(?<!\S)' + re.escape(key) + r'="[^"]*"')
    replacement = f'{key}="{value}"'
    if pattern.search(header):
        return pattern.sub(lambda _: replacement, header, count=1)
    return header.rstrip() + " " + replacement


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def source_urls(guides):
    """Recoge fuentes declaradas como XML/XMLTV/GZIP."""
    found = {}
    for guide in guides:
        channel_id = str(guide.get("channel") or "")
        for source in guide.get("sources") or []:
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "").strip()
            fmt = str(source.get("format") or "").upper()
            if not url or not url.startswith(("http://", "https://")):
                continue
            if fmt not in {"XML", "XMLTV", "GZIP", "XML.GZ"}:
                continue
            found.setdefault(url, {
                "url": url,
                "format": fmt,
                "guide_channels": set(),
            })
            if channel_id:
                found[url]["guide_channels"].add(channel_id)
    return list(found.values())


def parse_xmltv(data):
    """Devuelve raíz XMLTV; rechaza HTML y XML malformado."""
    root = ET.fromstring(data)
    if root.tag.split("}")[-1].lower() != "tv":
        raise ValueError("El documento no tiene una raíz XMLTV <tv>.")
    return root


def child_text(element, wanted):
    for child in element:
        if child.tag.split("}")[-1].lower() == wanted:
            return (child.text or "").strip()
    return ""


def resolve_target_id(source_id, valid_ids):
    """Resuelve IDs XMLTV con sufijos como @SD/@HD sin alterar la playlist."""
    source_id = str(source_id or "").strip()
    if not source_id:
        return None
    if source_id in valid_ids:
        return source_id
    base = source_id.split("@", 1)[0]
    candidates = [candidate for candidate in valid_ids
                  if candidate.split("@", 1)[0] == base]
    if len(candidates) == 1:
        return candidates[0]
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="output")
    args = parser.parse_args()

    m3u_url = os.environ.get("M3U_URL", "").strip()
    if not m3u_url:
        print("ERROR: falta el secreto M3U_URL.", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print("Descargando lista M3U...")
    playlist_text = download(m3u_url).decode("utf-8-sig")
    lines = playlist_text.splitlines()
    if not any(line.strip().startswith("#EXTM3U") for line in lines):
        raise ValueError("La URL M3U no devolvió una playlist válida.")

    print("Descargando catálogos...")
    channels = get_json(CHANNELS_URL)
    guides = get_json(GUIDES_URL)
    feeds = get_json(FEEDS_URL)
    logos = get_json(LOGOS_URL)

    # Catálogos auxiliares: no se usan para adivinar identidades ambiguas.
    countries = get_json(COUNTRIES_URL)
    languages = get_json(LANGUAGES_URL)
    timezones = get_json(TIMEZONES_URL)

    channels_by_id = {
        str(channel.get("id")): channel for channel in channels if channel.get("id")
    }
    feeds_by_channel = defaultdict(list)
    feed_index = defaultdict(list)

    for feed in feeds:
        channel_id = str(feed.get("channel") or "")
        if channel_id:
            feeds_by_channel[channel_id].append(feed)
        for name in names_for_feed(feed):
            key = norm(name)
            if key:
                feed_index[key].append(feed)

    index = defaultdict(list)
    for channel in channels:
        for name in names_for(channel):
            key = norm(name)
            if key and channel not in index[key]:
                index[key].append(channel)

    # Primera pasada: completar identificadores en la playlist.
    entries = []
    rewritten = []
    current = None
    total = 0
    matched = 0

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#EXTINF:"):
            total += 1
            header, display_name = split_extinf(stripped)
            attrs = attributes(header)
            match = match_channel(display_name, index)

            # Si coincide con un feed regional exacto, usar su canal padre.
            if not match:
                feed_candidates = feed_index.get(norm(display_name), [])
                feed_channels = {
                    str(feed.get("channel") or "") for feed in feed_candidates
                    if str(feed.get("channel") or "") in channels_by_id
                }
                if len(feed_channels) == 1:
                    match = channels_by_id[next(iter(feed_channels))]

            tvg_id = attrs.get("tvg-id", "").strip()
            tvg_name = attrs.get("tvg-name", "").strip()

            matched_feed = None
            catalog_id = str((match or {}).get("id") or "")
            feed_id = ""

            if match:
                matched += 1
                if catalog_id and tvg_id != catalog_id:
                    tvg_id = catalog_id
                    header = set_attr(header, "tvg-id", tvg_id)

                if not tvg_name:
                    tvg_name = str(match.get("name") or display_name)
                    if tvg_name:
                        header = set_attr(header, "tvg-name", tvg_name)

                possible_feeds = [
                    f for f in feed_index.get(norm(display_name), [])
                    if str(f.get("channel") or "") == catalog_id
                ]
                if len(possible_feeds) == 1:
                    matched_feed = possible_feeds[0]
                    feed_id = str(matched_feed.get("id") or "")

                if not attributes(header).get("tvg-logo"):
                    logo = best_logo(catalog_id, feed_id, logos)
                    if logo:
                        header = set_attr(header, "tvg-logo", logo)

            current = {
                "name": display_name,
                "id": tvg_id,
                "tvg_name": tvg_name,
                "catalog_id": catalog_id,
                "feed_id": feed_id,
                "country": str((match or {}).get("country") or ""),
                "catalog_names": names_for(match) if match else [],
            }
            entries.append(current)
            rewritten.append("#EXTINF:" + header + "," + display_name)
        else:
            rewritten.append(line)

    # Relaciona nombres de la playlist con sus IDs finales.
    name_to_ids = defaultdict(set)
    id_to_names = defaultdict(set)
    valid_ids = set()

    for entry in entries:
        channel_id = entry["id"]
        if channel_id:
            valid_ids.add(channel_id)
            extra_names = []

            if entry.get("feed_id"):
                for feed in feeds_by_channel.get(entry.get("catalog_id", ""), []):
                    if str(feed.get("id") or "") == entry["feed_id"]:
                        extra_names.extend(names_for_feed(feed))
                        break

            for name in (
                entry["name"],
                entry["tvg_name"],
                *entry.get("catalog_names", []),
                *extra_names,
            ):
                if name:
                    name_to_ids[norm(name)].add(channel_id)
                    id_to_names[channel_id].add(name)

            base_id = channel_id.split("@", 1)[0]
            if base_id:
                id_to_names[channel_id].add(base_id)
                
    # Añade la URL pública de la guía a la cabecera M3U.
    public_epg_url = (
        "https://raw.githubusercontent.com/"
        "Rayder378/m3u-epg-auto-/main/output/epg.xml"
    )

    for i, line in enumerate(rewritten):
        if line.strip().startswith("#EXTM3U"):
            header = line.strip()
            header = re.sub(r'\s+x-tvg-url="[^"]*"', "", header)
            rewritten[i] = header + f' x-tvg-url="{public_epg_url}"'
            break

    (out / "playlist_epg.m3u").write_text(
        "\n".join(rewritten).rstrip() + "\n", encoding="utf-8"
    )

    # Descarga fuentes y fusiona solamente los canales reconocibles.
    print("Buscando fuentes XMLTV...")
    sources = source_urls(guides)
    xml_channels = {}
    programmes = {}
    successful_sources = []
    source_report = []
    failures = 0

    for source in sources:
        url = source["url"]
        try:
            print("Probando fuente:", urlparse(url).netloc)
            root = parse_xmltv(download(url))
            found_channels = 0
            found_programmes = 0

            # Vincula IDs XMLTV con los IDs de la playlist por ID o nombre.
            local_id_map = {}
            for channel in root:
                if channel.tag.split("}")[-1].lower() != "channel":
                    continue

                xml_id = str(channel.get("id") or "").strip()
                if not xml_id:
                    continue

                target_id = resolve_target_id(xml_id, valid_ids)

                if not target_id:
                    # También busca coincidencias exactas por display-name.
                    for display in channel:
                        if display.tag.split("}")[-1].lower() != "display-name":
                            continue
                        display_name = (display.text or "").strip()
                        ids = name_to_ids.get(norm(display_name), set())
                        if len(ids) == 1:
                            target_id = next(iter(ids))
                            break
                        # No usar coincidencias parciales: pueden mezclar canales.

                if target_id:
                    local_id_map[xml_id] = target_id
                    if target_id not in xml_channels:
                        clone = ET.fromstring(ET.tostring(channel))
                        clone.set("id", target_id)
                        xml_channels[target_id] = clone
                        found_channels += 1

            for programme in root:
                if programme.tag.split("}")[-1].lower() != "programme":
                    continue

                source_id = str(programme.get("channel") or "").strip()
                target_id = local_id_map.get(source_id)
                if not target_id:
                    continue

                clone = ET.fromstring(ET.tostring(programme))
                clone.set("channel", target_id)
                start = clone.get("start", "")
                stop = clone.get("stop", "")
                title = child_text(clone, "title")
                key = (target_id, start, stop, title)

                if key not in programmes:
                    programmes[key] = clone
                    found_programmes += 1

            successful_sources.append(url)
            source_report.append({
                "url": url,
                "formato": source["format"],
                "estado": "XMLTV válido",
                "canales_encontrados": found_channels,
                "programas_nuevos": found_programmes,
                "error": "",
            })

        except Exception as exc:
            failures += 1
            source_report.append({
                "url": url,
                "formato": source["format"],
                "estado": "fallo",
                "canales_encontrados": 0,
                "programas_nuevos": 0,
                "error": str(exc)[:300],
            })
            print("Fuente no utilizable:", urlparse(url).netloc, "-", exc)

    # Ordena por hora de inicio y construye un XMLTV estándar.
    tv = ET.Element("tv", {"generator-info-name": "m3u-epg-auto"})

    for channel_id in sorted(xml_channels):
        tv.append(xml_channels[channel_id])

    for key in sorted(programmes, key=lambda item: (item[0], item[1], item[2])):
        tv.append(programmes[key])

    ET.indent(tv, space="  ")
    xml_path = out / "epg.xml"

    # No pisa una guía previa válida si la ejecución no encontró programas.
    if not programmes and xml_path.exists():
        try:
            previous = parse_xmltv(xml_path.read_bytes())
            previous_programmes = sum(
                1 for node in previous
                if node.tag.split("}")[-1].lower() == "programme"
            )

            if previous_programmes:
                print(
                    "AVISO: la ejecución actual no encontró programas; "
                    "se conserva la guía anterior con programación."
                )
            else:
                ET.ElementTree(tv).write(
                    xml_path, encoding="utf-8", xml_declaration=True
                )

        except Exception:
            ET.ElementTree(tv).write(
                xml_path, encoding="utf-8", xml_declaration=True
            )

    else:
        ET.ElementTree(tv).write(
            xml_path, encoding="utf-8", xml_declaration=True
    )
        
    fields = [
        "canal_lista", "tvg_id", "tvg_name", "catalog_id",
        "pais_catalogo", "feed_id", "coincidencia", "guias_catalogo",
        "programas_encontrados",
    ]

    report = []
    for entry in entries:
        channel_id = entry["id"]
        count = sum(1 for key in programmes if key[0] == channel_id)

        report.append({
            "canal_lista": entry["name"],
            "tvg_id": channel_id,
            "tvg_name": entry["tvg_name"],
            "catalog_id": entry.get("catalog_id", ""),
            "pais_catalogo": entry.get("country", ""),
            "feed_id": entry.get("feed_id", ""),
            "coincidencia": "sí" if channel_id else "sin ID",
            "guias_catalogo": sum(
                1 for guide in guides
                if channel_id and str(guide.get("channel") or "")
                and (
                    str(guide.get("channel")) == channel_id
                    or str(guide.get("channel")).split("@", 1)[0]
                    == channel_id.split("@", 1)[0]
                )
            ),
            "programas_encontrados": count,
        })

    write_csv(out / "informe_epg.csv", fields, report)
    (out / "informe_epg.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    write_csv(
        out / "fuentes_epg.csv",
        [
            "url",
            "formato",
            "estado",
            "canales_encontrados",
            "programas_nuevos",
            "error",
        ],
        source_report,
    )

    (out / "fuentes_epg.json").write_text(
        json.dumps(source_report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Catálogo de países cargado: {len(countries)}")
    print(f"Catálogo de idiomas cargado: {len(languages)}")
    print(f"Catálogo de zonas horarias cargado: {len(timezones)}")
    print(f"Feeds regionales cargados: {len(feeds)}")
    print(f"Logotipos catalogados: {len(logos)}")
    print(f"Canales analizados: {total}")
    print(f"Canales con coincidencia de catálogo: {matched}")
    print(f"Fuentes XMLTV probadas: {len(sources)}")
    print(f"Fuentes con XMLTV válido: {len(successful_sources)}")
    print(f"Canales en la guía generada: {len(xml_channels)}")
    print(f"Programas en la guía generada: {len(programmes)}")
    print(f"Fuentes con error: {failures}")
    print(f"Playlist generada: {out / 'playlist_epg.m3u'}")
    print(f"XMLTV generado: {xml_path}")

    if not programmes:
        print(
            "AVISO: no se encontraron programas coincidentes. "
            "Revisa fuentes_epg.csv antes de configurar el reproductor."
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
        
