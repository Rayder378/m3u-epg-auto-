
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Enriquece una lista M3U con tvg-id y tvg-name utilizando
el catálogo oficial de canales y fuentes EPG de IPTV-org.

Variables de entorno:
    M3U_URL: URL privada de la lista M3U.
    XMLTV_URL: opcional; URL de una guía XMLTV, si dispones de ella.

Uso:
    python m3u_epg_matcher.py --out output
"""

import argparse
import csv
import gzip
import io
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path


CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
GUIDES_URL = "https://iptv-org.github.io/api/guides.json"

USER_AGENT = "m3u-epg-auto/1.0"
TIMEOUT = 60


def download(url):
    """Descarga contenido HTTP y admite respuestas comprimidas con gzip."""
    if not url:
        raise ValueError("La URL está vacía.")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                data = gzip.decompress(data)
            return data
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Error HTTP {exc.code} al descargar una fuente."
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"No se pudo descargar una fuente: {exc.reason}"
        ) from exc


def read_json(url):
    """Descarga y valida un catálogo JSON."""
    data = download(url)
    try:
        result = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"El catálogo JSON no es válido: {url}") from exc

    if not isinstance(result, list):
        raise RuntimeError(f"Se esperaba una lista JSON: {url}")

    return result


def normalize(value):
    """Normaliza nombres para comparar diferencias de mayúsculas y tildes."""
    value = str(value or "").strip().casefold()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.replace("&", " y ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def channel_names(channel):
    """Obtiene el nombre principal y los nombres alternativos."""
    names = [channel.get("name", "")]

    alternatives = channel.get("alt_names") or []
    if isinstance(alternatives, str):
        alternatives = [alternatives]

    names.extend(alternatives)
    return [name.strip() for name in names if isinstance(name, str) and name.strip()]


def build_channel_index(channels):
    """Relaciona nombres normalizados con canales del catálogo."""
    index = defaultdict(list)

    for channel in channels:
        channel_id = channel.get("id")
        if not channel_id:
            continue

        for name in channel_names(channel):
            key = normalize(name)
            if key and channel not in index[key]:
                index[key].append(channel)

    return index


def match_channel(name, channel_index):
    """Devuelve una coincidencia exacta solo si es inequívoca."""
    key = normalize(name)
    if not key:
        return None, "nombre_vacio"

    candidates = channel_index.get(key, [])

    # Un mismo nombre puede estar asociado a varios canales.
    unique = {}
    for candidate in candidates:
        unique[candidate.get("id")] = candidate

    if len(unique) == 1:
        return next(iter(unique.values())), "coincidencia_exacta"

    if len(unique) > 1:
        return None, "nombre_ambiguo"

    return None, "sin_coincidencia"


def split_extinf(line):
    """Separa la cabecera EXTINF del nombre visible."""
    content = line[len("#EXTINF:"):]

    # La coma separadora debe estar fuera de las comillas.
    quoted = False
    comma_index = -1

    for index, char in enumerate(content):
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            comma_index = index
            break

    if comma_index < 0:
        return content, ""

    return content[:comma_index], content[comma_index + 1:].strip()


ATTRIBUTE_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


def parse_attributes(header):
    """Lee atributos entre comillas de la cabecera EXTINF."""
    return {
        match.group(1): match.group(2)
        for match in ATTRIBUTE_RE.finditer(header)
    }


def set_attribute(header, key, value):
    """Añade o reemplaza un atributo sin borrar los demás."""
    pattern = re.compile(
        r'(?<!\S)' + re.escape(key) + r'="[^"]*"'
    )
    replacement = f'{key}="{value}"'

    if pattern.search(header):
        return pattern.sub(lambda _: replacement, header, count=1)

    return header.rstrip() + " " + replacement


def csv_write(path, fieldnames, rows):
    """Escribe CSV compatible con Excel."""
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def guide_source_records(guides, playlist_names_by_id):
    """Registra todas las fuentes URL anunciadas en guides.json."""
    records = []

    for guide in guides:
        channel_id = str(guide.get("channel") or "")
        playlist_names = playlist_names_by_id.get(channel_id, [])

        sources = guide.get("sources") or []
        for source in sources:
            if not isinstance(source, dict):
                continue

            url = str(source.get("url") or "").strip()
            if not url:
                continue

            records.append({
                "canal_lista": " | ".join(sorted(set(playlist_names))),
                "tvg_id": channel_id,
                "canal_catalogo": str(
                    guide.get("site_name")
                    or guide.get("site")
                    or channel_id
                ),
                "sitio": str(guide.get("site") or ""),
                "nombre_guia": str(guide.get("feed") or ""),
                "idioma": str(guide.get("lang") or ""),
                "formato": str(source.get("format") or ""),
                "url_fuente": url,
            })

    return records


def main():
    parser = argparse.ArgumentParser(
        description="Añade identificadores de canales y genera informes EPG."
    )
    parser.add_argument("--out", default="output", help="Carpeta de salida")
    args = parser.parse_args()

    m3u_url = os.environ.get("M3U_URL", "").strip()
    if not m3u_url:
        print("ERROR: falta configurar el secreto M3U_URL.", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Descargando lista M3U...")
    raw_playlist = download(m3u_url).decode("utf-8-sig")
    lines = raw_playlist.splitlines()

    if not any(line.strip().startswith("#EXTM3U") for line in lines):
        print("ERROR: la respuesta descargada no parece una lista M3U.")
        return 1

    # Conserva una copia de la lista original descargada en esta ejecución.
    backup_path = out_dir / "IejmoloV_backup.m3u"
    backup_path.write_text(raw_playlist.rstrip() + "\n", encoding="utf-8")

    print("Descargando catálogo de canales y fuentes EPG...")
    channels = read_json(CHANNELS_URL)
    guides = read_json(GUIDES_URL)

    channel_index = build_channel_index(channels)

    guides_by_channel = defaultdict(list)
    for guide in guides:
        channel_id = str(guide.get("channel") or "")
        if channel_id:
            guides_by_channel[channel_id].append(guide)

    output_lines = []
    report_rows = []
    playlist_names_by_id = defaultdict(list)

    total_channels = 0
    matched_channels = 0
    ambiguous_channels = 0
    current_extinf = None

    for line in lines:
        stripped = line.strip()

        if not stripped.startswith("#EXTINF:"):
            output_lines.append(line)
            continue

        total_channels += 1
        header, display_name = split_extinf(stripped)
        attrs = parse_attributes(header)

        # Busca por el nombre visible de la lista, no depende de que
        # el M3U original tenga tvg-id o tvg-name.
        match, match_status = match_channel(display_name, channel_index)

        old_id = attrs.get("tvg-id", "").strip()
        old_name = attrs.get("tvg-name", "").strip()

        if match:
            canonical_id = str(match.get("id") or "")
            canonical_name = str(match.get("name") or display_name)

            # Solo completa los campos vacíos. No sobrescribe valores
            # que ya existían en la lista de entrada.
            if not old_id and canonical_id:
                header = set_attribute(header, "tvg-id", canonical_id)

            if not old_name and canonical_name:
                header = set_attribute(header, "tvg-name", canonical_name)

            lookup_id = canonical_id or old_id
            matched_channels += 1
            if lookup_id:
                playlist_names_by_id[lookup_id].append(display_name)
        else:
            lookup_id = old_id
            if match_status == "nombre_ambiguo":
                ambiguous_channels += 1

        output_lines.append("#EXTINF:" + header + "," + display_name)

        matched_guides = guides_by_channel.get(lookup_id, [])
        source_count = sum(
            len([
                source for source in (guide.get("sources") or [])
                if isinstance(source, dict) and source.get("url")
            ])
            for guide in matched_guides
        )

        report_rows.append({
            "canal_lista": display_name,
            "tvg_id": lookup_id,
            "tvg_name": attrs.get("tvg-name", "")
                or (str(match.get("name") or "") if match else ""),
            "coincidencia": match_status,
            "canal_encontrado": "sí" if match else "no",
            "guias_catalogo": len(matched_guides),
            "fuentes_epg": source_count,
        })

    playlist_path = out_dir / "playlist_epg.m3u"
    playlist_path.write_text(
        "\n".join(output_lines).rstrip() + "\n",
        encoding="utf-8",
    )

    report_fields = [
        "canal_lista",
        "tvg_id",
        "tvg_name",
        "coincidencia",
        "canal_encontrado",
        "guias_catalogo",
        "fuentes_epg",
    ]

    csv_write(out_dir / "informe_epg.csv", report_fields, report_rows)

    (out_dir / "informe_epg.json").write_text(
        json.dumps(report_rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    source_rows = guide_source_records(guides, playlist_names_by_id)
    source_fields = [
        "canal_lista",
        "tvg_id",
        "canal_catalogo",
        "sitio",
        "nombre_guia",
        "idioma",
        "formato",
        "url_fuente",
    ]

    csv_write(out_dir / "fuentes_epg.csv", source_fields, source_rows)

    (out_dir / "fuentes_epg.json").write_text(
        json.dumps(source_rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Canales analizados: {total_channels}")
    print(f"Canales con coincidencia exacta: {matched_channels}")
    print(f"Canales con nombre ambiguo: {ambiguous_channels}")
    print(f"Guías en el catálogo: {len(guides)}")
    print(f"Fuentes registradas: {len(source_rows)}")
    print(f"Lista generada: {playlist_path}")
    print(f"Informe generado: {out_dir / 'informe_epg.csv'}")
    print(f"Fuentes EPG: {out_dir / 'fuentes_epg.csv'}")

    # XMLTV_URL es opcional. Se informa si está configurada, pero
    # no se modifica ni se sustituye la guía con una URL no validada.
    if os.environ.get("XMLTV_URL", "").strip():
        print(
            "XMLTV_URL está configurada. La descarga de la guía XMLTV "
            "no forma parte de esta versión del script."
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
