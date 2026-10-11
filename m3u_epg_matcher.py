
import argparse
import csv
import gzip
import io
import json
import os
import re
import sys
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
GUIDES_URL = "https://iptv-org.github.io/api/guides.json"
USER_AGENT = "IPTV-EPG-Matcher/1.0"


def download(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def decode_data(data):
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def normalize(value):
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", value.lower())


def read_json(url):
    return json.loads(download(url).decode("utf-8-sig"))


def parse_attributes(line):
    return {
        key.lower(): value
        for key, value in re.findall(
            r'([\w-]+)="([^"]*)"', line
        )
    }


def set_attribute(line, key, value):
    pattern = re.compile(
        r'(?i)(?<![\w-])' + re.escape(key) + r'="[^"]*"'
    )
    replacement = f'{key}="{value}"'
    if pattern.search(line):
        return pattern.sub(replacement, line, count=1)
    return line.replace("#EXTINF:", "#EXTINF:" + replacement + " ", 1)


def channel_names(channel):
    names = [channel.get("name", "")]
    names.extend(channel.get("alt_names") or [])
    return {normalize(name) for name in names if normalize(name)}


def build_index(channels):
    index = {}
    for channel in channels:
        for name in channel_names(channel):
            index.setdefault(name, []).append(channel)
    return index


def unique_match(name, index):
    matches = index.get(normalize(name), [])
    unique = {item["id"]: item for item in matches}
    if len(unique) == 1:
        return next(iter(unique.values()))
    return None


def playlist_entries(lines):
    entries = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXTINF:"):
            continue
        title = line.rsplit(",", 1)[-1].strip()
        attrs = parse_attributes(line)
        name = attrs.get("tvg-name") or title
        entries.append({
            "line_index": i,
            "title": title,
            "name": name,
            "attrs": attrs,
        })
    return entries


def xmltv_channels(root):
    result = {}
    for channel in root.findall("channel"):
        names = {
            normalize(node.text or "")
            for node in channel.findall("display-name")
            if normalize(node.text or "")
        }
        if names:
            result[channel.get("id", "")] = names
    return result


def make_xmltv(input_url, output_path, entries):
    raw = decode_data(download(input_url))
    root = ET.parse(io.BytesIO(raw)).getroot()

    if root.tag != "tv":
        raise ValueError("El archivo descargado no parece XMLTV.")

    guide_channels = xmltv_channels(root)
    by_name = {}

    for channel_id, names in guide_channels.items():
        for name in names:
            by_name.setdefault(name, set()).add(channel_id)

    # Relacionar cada canal de la lista con IDs reales de XMLTV.
    id_map = {}
    for entry in entries:
        candidates = by_name.get(normalize(entry["name"]), set())
        if len(candidates) == 1:
            xmltv_id = next(iter(candidates))
            old_id = entry["attrs"].get("tvg-id", "")
            id_map[old_id] = xmltv_id
            entry["xmltv_id"] = xmltv_id

    # Cambiar las referencias de programas solo para IDs inequívocos.
    for channel in root.findall("channel"):
        old_id = channel.get("id", "")
        if old_id in id_map:
            channel.set("id", id_map[old_id])

    for programme in root.findall("programme"):
        old_id = programme.get("channel", "")
        if old_id in id_map:
            programme.set("channel", id_map[old_id])

    # Quitar definiciones duplicadas que pudieran aparecer tras el mapeo.
    seen = set()
    for channel in list(root.findall("channel")):
        channel_id = channel.get("id", "")
        if channel_id in seen:
            root.remove(channel)
        else:
            seen.add(channel_id)

    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)

    return len(root.findall("programme"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--m3u", default=os.getenv("M3U_URL"))
    parser.add_argument("--xmltv", default=os.getenv("XMLTV_URL"))
    parser.add_argument("--out", default="output")
    args = parser.parse_args()

    if not args.m3u:
        sys.exit("ERROR: configura el secreto M3U_URL.")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Descargando lista M3U...")
    original_bytes = download(args.m3u)
    original_text = original_bytes.decode("utf-8-sig")
    lines = original_text.splitlines()

    if not lines or not lines[0].strip().startswith("#EXTM3U"):
        sys.exit("ERROR: la URL no devolvió una lista M3U válida.")

    # Copia de seguridad de la lista de origen en cada ejecución.
    backup = out_dir / "IejmoloV_backup.m3u"
    backup.write_text(original_text, encoding="utf-8")

    print("Descargando catálogo de canales y fuentes EPG...")
    channels = read_json(CHANNELS_URL)
    guides = read_json(GUIDES_URL)
    channel_index = build_index(channels)
    entries = playlist_entries(lines)

    report = []
    source_report = []
    updated_lines = list(lines)

    # Índice de guías por identificador de canal.
    guides_by_channel = {}
    for guide in guides:
        channel_id = guide.get("channel")
        if channel_id:
            guides_by_channel.setdefault(channel_id, []).append(guide)

    for entry in entries:
        attrs = entry["attrs"]
        old_id = attrs.get("tvg-id", "").strip()
        match = unique_match(entry["name"], channel_index)

        final_id = old_id
        action = "conservado"
        channel_id = old_id or ""

        if not old_id and match:
            final_id = match["id"]
            channel_id = match["id"]
            updated_lines[entry["line_index"]] = set_attribute(
                updated_lines[entry["line_index"]], "tvg-id", final_id
            )
            action = "completado"
        elif not old_id:
            action = "sin_coincidencia_unica"

        if match and not channel_id:
            channel_id = match["id"]

        available_guides = guides_by_channel.get(channel_id, [])
        source_urls = []

        for guide in available_guides:
            for source in guide.get("sources", []):
                url = source.get("url", "")
                if url:
                    source_urls.append(url)
                    source_report.append({
                        "canal_lista": entry["name"],
                        "tvg_id": final_id,
                        "canal_catalogo": channel_id,
                        "sitio": guide.get("site", ""),
                        "nombre_guia": guide.get("site_name", ""),
                        "idioma": guide.get("lang", ""),
                        "formato": source.get("format", ""),
                        "url_fuente": url,
                    })

        report.append({
            "canal": entry["name"],
            "tvg_id_original": old_id,
            "tvg_id_final": final_id,
            "coincidencia": (
                "coincidencia_unica" if match else "no_unica_o_no_encontrada"
            ),
            "accion": action,
            "nombre_catalogo": match.get("name", "") if match else "",
            "pais": match.get("country", "") if match else "",
            "estado_epg": (
                "fuente_registrada" if source_urls
                else "sin_fuente_registrada"
            ),
            "fuentes_epg": ";".join(sorted(set(source_urls))),
        })

    playlist_path = out_dir / "playlist_epg.m3u"
    playlist_path.write_text(
        "\n".join(updated_lines) + "\n", encoding="utf-8"
    )

    with (out_dir / "informe_epg.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as file:
        fields = list(report[0].keys()) if report else [
            "canal", "tvg_id_original", "tvg_id_final",
            "coincidencia", "accion", "nombre_catalogo", "pais",
            "estado_epg", "fuentes_epg"
        ]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(report)

    (out_dir / "informe_epg.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    with (out_dir / "fuentes_epg.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as file:
        fields = [
            "canal_lista", "tvg_id", "canal_catalogo", "sitio",
            "nombre_guia", "idioma", "formato", "url_fuente"
        ]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(source_report)

    if args.xmltv:
        try:
            print("Descargando y comprobando XMLTV...")
            count = make_xmltv(
                args.xmltv, out_dir / "guide.xml", entries
            )
            print(f"Programas XMLTV procesados: {count}")
        except Exception as exc:
            print(f"AVISO: no se pudo generar guide.xml: {exc}")
            print("Revisa la URL XMLTV y el formato de la fuente.")

    print(f"Canales analizados: {len(entries)}")
    print(f"Lista generada: {playlist_path}")
    print(f"Informes generados en: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
    
