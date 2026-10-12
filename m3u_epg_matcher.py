
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Actualiza una playlist M3U y combina fuentes XMLTV sin eliminar canales."""

import argparse
import csv
import gzip
import json
import os
import re
import sys
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET

from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
GUIDES_URL = "https://iptv-org.github.io/api/guides.json"
USER_AGENT = "m3u-epg-auto/3.1"
TIMEOUT = 45
MAX_DOWNLOAD = 35 * 1024 * 1024

PUBLIC_EPG_URL = (
    "https://raw.githubusercontent.com/"
    "Rayder378/m3u-epg-auto-/main/output/epg.xml"
)


def download(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        data = response.read(MAX_DOWNLOAD + 1)

        if len(data) > MAX_DOWNLOAD:
            raise ValueError("Descarga superior a 35 MB.")

        if (
            response.headers.get("Content-Encoding", "").lower() == "gzip"
            or data[:2] == b"\x1f\x8b"
        ):
            data = gzip.decompress(data)

        return data


def get_json(url):
    result = json.loads(download(url).decode("utf-8-sig"))
    if not isinstance(result, list):
        raise ValueError("El catálogo no contiene una lista JSON.")
    return result


def norm(value):
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def names_for(channel):
    if not channel:
        return []

    names = [channel.get("name", "")]
    aliases = channel.get("alt_names") or []

    if isinstance(aliases, str):
        aliases = [aliases]

    names.extend(aliases)

    return [
        name.strip()
        for name in names
        if isinstance(name, str) and name.strip()
    ]


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
    return {
        match.group(1): match.group(2)
        for match in ATTR_RE.finditer(header)
    }


def set_attr(header, key, value):
    replacement = f'{key}="{value}"'
    pattern = re.compile(
        r'(?<!\S)' + re.escape(key) + r'="[^"]*"'
    )

    if pattern.search(header):
        return pattern.sub(
            lambda _: replacement, header, count=1
        )

    return header.rstrip() + " " + replacement


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fields,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def source_urls(guides):
    """Recoge las fuentes XMLTV declaradas en el catálogo."""
    found = {}

    for guide in guides:
        channel_id = str(guide.get("channel") or "")

        for source in guide.get("sources") or []:
            if not isinstance(source, dict):
                continue

            url = str(source.get("url") or "").strip()
            fmt = str(source.get("format") or "").upper()

            if not url.startswith(("http://", "https://")):
                continue

            if fmt not in {"XML", "XMLTV", "GZIP", "XML.GZ"}:
                continue

            if url not in found:
                found[url] = {
                    "url": url,
                    "format": fmt,
                    "guide_channels": set(),
                }

            if channel_id:
                found[url]["guide_channels"].add(channel_id)

    return list(found.values())


def parse_xmltv(data):
    root = ET.fromstring(data)

    if root.tag.split("}")[-1].lower() != "tv":
        raise ValueError("La raíz XML no es <tv>.")

    return root


def child_text(element, wanted):
    for child in element:
        if child.tag.split("}")[-1].lower() == wanted:
            return (child.text or "").strip()

    return ""


def match_channel(name, index):
    candidates = index.get(norm(name), [])
    unique = {
        str(channel.get("id")): channel
        for channel in candidates
        if channel.get("id")
    }

    if len(unique) == 1:
        return next(iter(unique.values()))

    return None


def resolve_target_id(source_id, valid_ids):
    source_id = str(source_id or "").strip()

    if source_id in valid_ids:
        return source_id

    base = source_id.split("@", 1)[0]
    candidates = [
        item for item in valid_ids
        if item.split("@", 1)[0] == base
    ]

    if len(candidates) == 1:
        return candidates[0]

    return None


def resolve_by_name(channel, name_to_ids):
    """Relaciona un canal XMLTV con un único canal de la playlist."""
    display_names = [
        (node.text or "").strip()
        for node in channel
        if node.tag.split("}")[-1].lower() == "display-name"
    ]

    # Primero, coincidencia exacta.
    for display_name in display_names:
        ids = name_to_ids.get(norm(display_name), set())

        if len(ids) == 1:
            return next(iter(ids))

    # Después, coincidencia parcial solo si es inequívoca.
    possible = set()

    for display_name in display_names:
        name = norm(display_name)

        if len(name) < 5:
            continue

        for known_name, ids in name_to_ids.items():
            if len(known_name) < 5:
                continue

            if name in known_name or known_name in name:
                possible.update(ids)

    if len(possible) == 1:
        return next(iter(possible))

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

    print("Descargando playlist...")
    playlist_text = download(m3u_url).decode("utf-8-sig")
    lines = playlist_text.splitlines()

    if not any(line.strip().startswith("#EXTM3U") for line in lines):
        raise ValueError("La URL no devolvió una playlist M3U válida.")

    print("Descargando catálogos...")
    channels = get_json(CHANNELS_URL)
    guides = get_json(GUIDES_URL)

    index = defaultdict(list)

    for channel in channels:
        for name in names_for(channel):
            key = norm(name)

            if key and channel not in index[key]:
                index[key].append(channel)

    entries = []
    rewritten = []
    total = 0
    matched = 0

    # Reservar los IDs existentes para evitar colisiones.
    reserved_ids = set()

    for original_line in lines:
        if original_line.strip().startswith("#EXTINF:"):
            original_header, _ = split_extinf(
                original_line.strip()
            )
            original_id = attributes(original_header).get(
                "tvg-id", ""
            ).strip()

            if original_id:
                reserved_ids.add(original_id)

    # Primera pasada: conservar todos los canales y completar metadatos.
    for line in lines:
        stripped = line.strip()

        if not stripped.startswith("#EXTINF:"):
            rewritten.append(line)
            continue

        total += 1
        header, display_name = split_extinf(stripped)
        attrs = attributes(header)

        existing_id = attrs.get("tvg-id", "").strip()
        existing_name = attrs.get("tvg-name", "").strip()
        match = match_channel(display_name, index)

        channel_id = existing_id
        tvg_name = existing_name

        if match:
            matched += 1

            if not channel_id:
                channel_id = str(match.get("id") or "").strip()

            if not tvg_name:
                tvg_name = str(
                    match.get("name") or display_name
                ).strip()

        # Crear un ID propio cuando el catálogo no aporta uno.
        if not channel_id:
            slug = re.sub(
                r"[^a-z0-9]+",
                "-",
                norm(display_name),
            ).strip("-")[:60]

            if not slug:
                slug = f"canal-{total}"

            base_id = f"m3u-auto-{slug}"
            channel_id = base_id
            suffix = 2

            while channel_id in reserved_ids:
                channel_id = f"{base_id}-{suffix}"
                suffix += 1

            reserved_ids.add(channel_id)

        if not tvg_name:
            tvg_name = display_name

        header = set_attr(header, "tvg-id", channel_id)
        header = set_attr(header, "tvg-name", tvg_name)

        entries.append({
            "name": display_name,
            "id": channel_id,
            "tvg_name": tvg_name,
            "catalog_id": str((match or {}).get("id") or ""),
            "catalog_names": names_for(match),
        })

        # Solo se cambia el encabezado EXTINF.
        # La URL de reproducción original permanece intacta.
        rewritten.append(
            "#EXTINF:" + header + "," + display_name
        )

    # Índices de nombres e identificadores finales de la playlist.
    name_to_ids = defaultdict(set)
    valid_ids = set()

    for entry in entries:
        channel_id = entry["id"]

        if not channel_id:
            continue

        valid_ids.add(channel_id)

        for name in (
            entry["name"],
            entry["tvg_name"],
            *entry["catalog_names"],
        ):
            if name:
                name_to_ids[norm(name)].add(channel_id)

    # Añadir la URL pública de la guía al encabezado M3U.
    for i, line in enumerate(rewritten):
        if line.strip().startswith("#EXTM3U"):
            header = line.strip()
            header = re.sub(
                r'\s+x-tvg-url="[^"]*"',
                "",
                header,
            )
            rewritten[i] = (
                header + f' x-tvg-url="{PUBLIC_EPG_URL}"'
            )
            break

    playlist_path = out / "playlist_epg.m3u"
    playlist_path.write_text(
        "\n".join(rewritten).rstrip() + "\n",
        encoding="utf-8",
    )

    # Descargar fuentes XMLTV y combinar sus canales y programas.
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

            local_id_map = {}
            found_channels = 0
            found_programmes = 0

            # Relacionar cada canal XMLTV con la playlist.
            for channel in root:
                if channel.tag.split("}")[-1].lower() != "channel":
                    continue

                xml_id = str(channel.get("id") or "").strip()

                if not xml_id:
                    continue

                target_id = resolve_target_id(xml_id, valid_ids)

                if not target_id:
                    target_id = resolve_by_name(
                        channel, name_to_ids
                    )

                if not target_id:
                    continue

                local_id_map[xml_id] = target_id

                if target_id not in xml_channels:
                    clone = ET.fromstring(
                        ET.tostring(channel)
                    )
                    clone.set("id", target_id)
                    xml_channels[target_id] = clone
                    found_channels += 1

            # Asociar programas solo a canales ya relacionados.
            for programme in root:
                if programme.tag.split("}")[-1].lower() != "programme":
                    continue

                source_id = str(
                    programme.get("channel") or ""
                ).strip()
                target_id = local_id_map.get(source_id)

                if not target_id:
                    continue

                clone = ET.fromstring(
                    ET.tostring(programme)
                )
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

            print(
                "Fuente no utilizable:",
                urlparse(url).netloc,
                "-",
                str(exc)[:200],
            )

    # Construir el XMLTV unificado.
    tv = ET.Element("tv", {
        "generator-info-name": "m3u-epg-auto",
    })

    for channel_id in sorted(xml_channels):
        tv.append(xml_channels[channel_id])

    for key in sorted(
        programmes,
        key=lambda item: (item[0], item[1], item[2]),
    ):
        tv.append(programmes[key])

    ET.indent(tv, space="  ")
    xml_path = out / "epg.xml"

    # Si no se encuentran programas, no borrar una guía útil anterior.
    if not programmes and xml_path.exists():
        try:
            previous = parse_xmltv(xml_path.read_bytes())
            old_count = sum(
                1 for node in previous
                if node.tag.split("}")[-1].lower() == "programme"
            )

            if old_count:
                print(
                    "AVISO: no se encontraron programas nuevos; "
                    "se conserva la guía anterior."
                )
            else:
                ET.ElementTree(tv).write(
                    xml_path,
                    encoding="utf-8",
                    xml_declaration=True,
                )
        except Exception:
            print(
                "AVISO: no se pudo validar la guía anterior; "
                "se conserva el archivo existente."
            )
    else:
        ET.ElementTree(tv).write(
            xml_path,
            encoding="utf-8",
            xml_declaration=True,
        )

    # Informe por canal.
    report = []

    for entry in entries:
        channel_id = entry["id"]

        program_count = sum(
            1 for key in programmes if key[0] == channel_id
        )

        guide_count = 0

        if channel_id:
            guide_count = sum(
                1 for guide in guides
                if (
                    str(guide.get("channel") or "") == channel_id
                    or str(
                        guide.get("channel") or ""
                    ).split("@", 1)[0]
                    == channel_id.split("@", 1)[0]
                )
            )

        report.append({
            "canal_lista": entry["name"],
            "tvg_id": channel_id,
            "tvg_name": entry["tvg_name"],
            "coincidencia": (
                "sí"
                if entry["catalog_id"]
                else "sin coincidencia de catálogo"
            ),
            "guias_catalogo": guide_count,
            "programas_encontrados": program_count,
        })

    write_csv(
        out / "informe_epg.csv",
        [
            "canal_lista",
            "tvg_id",
            "tvg_name",
            "coincidencia",
            "guias_catalogo",
            "programas_encontrados",
        ],
        report,
    )

    (out / "informe_epg.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
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

    print(f"Canales analizados: {total}")
    print(f"Canales con coincidencia de catálogo: {matched}")
    print(f"Fuentes XMLTV probadas: {len(sources)}")
    print(f"Fuentes con XMLTV válido: {len(successful_sources)}")
    print(f"Canales en la guía generada: {len(xml_channels)}")
    print(f"Programas en la guía generada: {len(programmes)}")
    print(f"Fuentes con error: {failures}")
    print(f"Playlist generada: {playlist_path}")
    print(f"XMLTV generado: {xml_path}")

    if not programmes:
        print(
            "AVISO: no se encontraron programas coincidentes. "
            "Los canales de la playlist se conservan."
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
