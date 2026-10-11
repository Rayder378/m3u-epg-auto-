
import re
import gzip
import html
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import defaultdict
import argparse


def read_source(source):
    if source.startswith(("http://", "https://")):
        req = urllib.request.Request(
            source,
            headers={"User-Agent": "M3U-EPG-Matcher/1.0"}
        )
        with urllib.request.urlopen(req, timeout=60) as response:
            data = response.read()
    else:
        data = Path(source).read_bytes()

    if data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)
    return data


def normalize(name):
    name = html.unescape(name or "").casefold()
    name = re.sub(r"\b(uhd|fhd|hd|sd|4k|1080p|720p)\b", " ", name)
    return re.sub(r"[^a-z0-9]+", "", name)


def get_attr(line, attr):
    match = re.search(
        r'(?:^|\s)' + re.escape(attr) + r'="([^"]*)"',
        line
    )
    return html.unescape(match.group(1)) if match else ""


def set_attr(line, attr, value):
    value = html.escape(value, quote=True)
    pattern = r'(\s' + re.escape(attr) + r'=")[^"]*(")'

    if re.search(pattern, line):
        return re.sub(
            pattern,
            lambda m: m.group(1) + value + m.group(2),
            line,
            count=1
        )

    # Insert attribute before the display name.
    in_quote = False
    for i, char in enumerate(line):
        if char == '"':
            in_quote = not in_quote
        elif char == "," and not in_quote:
            return line[:i] + f' {attr}="{value}"' + line[i:]

    return line


def display_name(line):
    in_quote = False
    for i, char in enumerate(line):
        if char == '"':
            in_quote = not in_quote
        elif char == "," and not in_quote:
            return line[i + 1:].strip()
    return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--m3u", required=True)
    parser.add_argument("--xmltv", required=True)
    parser.add_argument("--out", default="output")
    args = parser.parse_args()

    m3u = read_source(args.m3u).decode("utf-8-sig", errors="replace")
    xml_data = read_source(args.xmltv)
    root = ET.fromstring(xml_data)

    # Index XMLTV display names by normalized name.
    index = defaultdict(set)

    for channel in root.findall("channel"):
        channel_id = channel.get("id", "").strip()
        if not channel_id:
            continue

        for item in channel.findall("display-name"):
            if item.text and item.text.strip():
                key = normalize(item.text)
                if key:
                    index[key].add(channel_id)

    lines = m3u.splitlines()
    output = []
    report = []
    matched = 0
    missing = 0
    i = 0

    for line in lines:
        if not line.startswith("#EXTINF:"):
            output.append(line)
            continue

        name = display_name(line)
        current_id = get_attr(line, "tvg-id")

        # Keep existing IDs unchanged.
        if current_id:
            output.append(line)
            continue

        candidates = index.get(normalize(name), set())

        if len(candidates) == 1:
            channel_id = next(iter(candidates))
            line = set_attr(line, "tvg-id", channel_id)
            line = set_attr(line, "tvg-name", name)
            matched += 1
        else:
            missing += 1
            reason = "ambiguous" if candidates else "not found"
            report.append(f"{name}: {reason}")

        output.append(line)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "playlist.m3u").write_text(
        "\n".join(output) + "\n",
        encoding="utf-8"
    )
    (out / "report.txt").write_text(
        f"IDs added: {matched}\n"
        f"Unmatched or ambiguous: {missing}\n\n"
        + "\n".join(report),
        encoding="utf-8"
    )

    print(f"IDs added: {matched}")
    print(f"Unmatched/ambiguous: {missing}")


if __name__ == "__main__":
    main()
      
