
import csv
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from urllib.request import Request, urlopen

CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
GUIDES_URL = "https://iptv-org.github.io/api/guides.json"

OUTPUT_DIR = "output"
OUTPUT_M3U = os.path.join(OUTPUT_DIR, "playlist_epg.m3u")
OUTPUT_CSV = os.path.join(OUTPUT_DIR, "informe_epg.csv")
OUTPUT_JSON = os.path.join(OUTPUT_DIR, "informe_epg.json")


def download_text(url):
    request = Request(
        url,
        headers={"User-Agent": "M3U-EPG-Matcher/1.0"}
    )
    with urlopen(request, timeout=90) as response:
        return response.read().decode("utf-8-sig", errors="replace")


def download_json(url):
    return json.loads(download_text(url))


def normalize_name(value):
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(
        char for char in value
        if not unicodedata.combining(char)
    )
    value = value.casefold()
    value = re.sub(r"\b(uhd|fhd|hd|sd|4k|8k)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def get_attribute(line, attribute):
    match = re.search(
        rf'(?:^|\s){re.escape(attribute)}\s*=\s*"([^"]*)"',
        line
    )
    return match.group(1).strip() if match else ""


def get_display_name(line):
    if "," in line:
        return line.split(",", 1)[1].strip()
    return ""


def add_tvg_id(line, channel_id):
    if not channel_id:
        return line

    pattern = r'(\btvg-id\s*=\s*)"[^"]*"'

    if re.search(pattern, line):
        return re.sub(
            pattern,
            lambda match: match.group(1) + '"' + channel_id + '"',
            line,
            count=1
        )

    match = re.match(r"^(#EXTINF:[^,]*)(,.*)$", line)

    if match:
        return (
            match.group(1)
            + f' tvg-id="{channel_id}"'
            + match.group(2)
        )

    return line


def build_channel_index(channels):
    index = defaultdict(set)
    by_id = {}

    for channel in channels:
        channel_id = channel.get("id")

        if not channel_id:
            continue

        by_id[channel_id] = channel

        names = [channel.get("name", "")]
        names.extend(channel.get("alt_names") or [])

        for name in names:
            normalized = normalize_name(name)

            if normalized:
                index[normalized].add(channel_id)

    return index, by_id


def build_guide_index(guides):
    index = defaultdict(list)

    for guide in guides:
        channel_id = guide.get("channel")

        if channel_id:
            index[channel_id].append(guide)

    return index


def find_match(names, channel_index):
    candidates = set()

    for name in names:
        normalized = normalize_name(name)

        if normalized:
            candidates.update(channel_index.get(normalized, set()))

    if len(candidates) == 1:
        return next(iter(candidates)), "coincidencia_unica"

    if len(candidates) > 1:
        return "", "coincidencia_ambigua"

    return "", "sin_coincidencia"


def get_guide_details(entries):
    urls = set()
    sites = set()

    for entry in entries:
        site = entry.get("site")
        site_name = entry.get("site_name")

        if site:
            sites.add(site)

        if site_name:
            sites.add(site_name)

        for source in entry.get("sources") or []:
            if isinstance(source, dict) and source.get("url"):
                urls.add(source["url"])

    return len(urls), "; ".join(sorted(sites)), "; ".join(sorted(urls))


def main():
    m3u_url = os.environ.get("M3U_URL", "").strip()

    if not m3u_url:
        raise ValueError(
            "Falta el secreto M3U_URL en Settings > Secrets and variables > Actions."
        )

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("Descargando lista M3U...")
    playlist = download_text(m3u_url)

    print("Descargando catálogo de canales...")
    channels = download_json(CHANNELS_URL)

    print("Descargando catálogo de fuentes EPG...")
    guides = download_json(GUIDES_URL)

    channel_index, channels_by_id = build_channel_index(channels)
    guides_by_id = build_guide_index(guides)

    output_lines = []
    report = []
    current_extinf_index = None
    channel_number = 0

    for line in playlist.splitlines():
        if line.startswith("#EXTINF:"):
            current_extinf_index = len(output_lines)
            output_lines.append(line)
            continue

        if (
            current_extinf_index is not None
            and line.strip()
            and not line.lstrip().startswith("#")
        ):
            channel_number += 1

            extinf = output_lines[current_extinf_index]
            display_name = get_display_name(extinf)
            tvg_name = get_attribute(extinf, "tvg-name")
            original_id = get_attribute(extinf, "tvg-id")

            names = [
                name for name in (tvg_name, display_name) if name
            ]

            suggested_id, match_status = find_match(
                names, channel_index
            )

            final_id = original_id
            action = "conservado"

            # Solo completar IDs vacíos; nunca sustituir los existentes.
            if not original_id and suggested_id:
                final_id = suggested_id
                action = "completado"

                output_lines[current_extinf_index] = add_tvg_id(
                    extinf, final_id
                )

            elif not original_id:
                action = "requiere_revision"

            channel_data = channels_by_id.get(final_id, {})
            guide_entries = guides_by_id.get(final_id, [])

            source_count, sites, urls = get_guide_details(guide_entries)

            if source_count:
                guide_status = "fuente_registrada_con_url"
            elif guide_entries:
                guide_status = "registro_sin_url"
            elif final_id:
                guide_status = "sin_fuente_registrada"
            else:
                guide_status = "sin_id"

            report.append({
                "numero": channel_number,
                "nombre_m3u": display_name,
                "tvg_name": tvg_name,
                "tvg_id_original": original_id,
                "tvg_id_final": final_id,
                "coincidencia": match_status,
                "accion": action,
                "nombre_catalogo": channel_data.get("name", ""),
                "pais": channel_data.get("country", ""),
                "estado_epg": guide_status,
                "cantidad_urls_fuente": source_count,
                "sitios_epg": sites,
                "urls_fuente_epg": urls,
            })

            output_lines.append(line)
            current_extinf_index = None
            continue

        output_lines.append(line)

    with open(OUTPUT_M3U, "w", encoding="utf-8", newline="\n") as file:
        file.write("\n".join(output_lines) + "\n")

    fields = [
        "numero",
        "nombre_m3u",
        "tvg_name",
        "tvg_id_original",
        "tvg_id_final",
        "coincidencia",
        "accion",
        "nombre_catalogo",
        "pais",
        "estado_epg",
        "cantidad_urls_fuente",
        "sitios_epg",
        "urls_fuente_epg",
    ]

    with open(
        OUTPUT_CSV, "w", encoding="utf-8-sig", newline=""
    ) as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(report)

    with open(OUTPUT_JSON, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)

    completed = sum(
        item["accion"] == "completado" for item in report
    )
    ambiguous = sum(
        item["coincidencia"] == "coincidencia_ambigua"
        for item in report
    )
    with_source = sum(
        item["estado_epg"] == "fuente_registrada_con_url"
        for item in report
    )

    print("\n--- RESUMEN ---")
    print(f"Canales procesados: {len(report)}")
    print(f"IDs completados: {completed}")
    print(f"Coincidencias ambiguas: {ambiguous}")
    print(f"Canales con URL de fuente EPG: {with_source}")
    print(f"Lista: {OUTPUT_M3U}")
    print(f"Informe CSV: {OUTPUT_CSV}")
    print(f"Informe JSON: {OUTPUT_JSON}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
              
