import re

def parse_lrc(lyrics_str):
    pattern = re.compile(r"\[(\d+):(\d+\.\d+)\](.*)")
    parsed = []

    for line in lyrics_str.splitlines():
        match = pattern.match(line)
        if match:
            minutes, seconds, text = match.groups()
            timestamp_ms = (int(minutes) * 60 + float(seconds)) * 1000
            parsed.append({"time": int(timestamp_ms), "text": text.strip()})
    return parsed

def get_synced_line(parsed_lyrics, progress_ms):
    current_line = None
    for line in parsed_lyrics:
        if line["time"] <= progress_ms:
            current_line = line
        else:
            break
    return current_line
