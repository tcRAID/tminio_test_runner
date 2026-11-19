"""Keep a bounded recent request trace for long endpoint soaks."""
import json


def append_event(path, event, max_bytes, backups=3):
    """Append one JSON event; the caller serializes writers using its state lock."""
    line = (json.dumps(event, default=str) + "\n").encode("utf-8")
    if max_bytes <= 0 or backups < 1:
        raise ValueError("request-log limits must be positive")
    if len(line) > max_bytes:
        raise ValueError("one request event exceeds the request-log limit")
    rotated = path.exists() and path.stat().st_size + len(line) > max_bytes
    if rotated:
        for number in range(backups, 1, -1):
            previous = path.with_name(path.name + "." + str(number - 1))
            if previous.exists():
                previous.replace(path.with_name(path.name + "." + str(number)))
        path.replace(path.with_name(path.name + ".1"))
    with path.open("ab") as output:
        output.write(line)
    return rotated
