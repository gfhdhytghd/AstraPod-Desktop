"""Read vCard 2.1, 3.0 and 4.0 and emit UTF-8 vCard 3.0 for the player."""
from __future__ import annotations

from datetime import datetime, timezone
import quopri
import re
from urllib.parse import unquote
from uuid import uuid4

from .extras_storage import fingerprint

FIELDS = {"FN": "显示名称", "N": "姓;名;其他名;前缀;后缀", "TEL": "电话",
          "EMAIL": "电子邮件", "ADR": "邮政信箱;扩展地址;街道;城市;省州;邮编;国家",
          "ORG": "组织;部门", "BDAY": "生日", "NOTE": "备注"}
STRUCTURED = {"N", "ADR", "ORG"}
REPEATED = {"TEL", "EMAIL", "ADR"}
PROP = re.compile(rb'^((?:[^":]|"[^"]*")+):(.*)$')
INLINE_PHOTO = re.compile(r"data:image/([a-z0-9.+-]+);base64,(.+)", re.I)


def _raw_lines(data):
    current, logical = bytearray(), bytearray()
    qp = encoded = False
    for line in data.splitlines(keepends=True):
        content = line.rstrip(b"\r\n")
        if qp and logical.endswith(b"="):
            del logical[-1:]
            logical.extend(content[1:] if content.startswith((b" ", b"\t")) else content)
        elif logical and content.startswith((b" ", b"\t")):
            logical.extend(content[1:])
        elif encoded and content and re.fullmatch(rb"[A-Za-z0-9+/=]+", content):
            logical.extend(content)
        else:
            if current:
                yield bytes(current), bytes(logical)
            current = bytearray()
            logical = bytearray(content)
            header = content.partition(b":")[0].upper()
            qp = b"ENCODING=QUOTED-PRINTABLE" in header
            encoded = bool(re.search(rb";ENCODING=(BASE64|B)(;|$)", header))
        current.extend(line)
    if current:
        yield bytes(current), bytes(logical)


def _lines(data):
    return (logical for _, logical in _raw_lines(data.removeprefix(b"\xef\xbb\xbf")) if logical)


def prop_name(header):
    return header.split(";", 1)[0].rsplit(".", 1)[-1].upper()


def _property(line, version):
    match = PROP.fullmatch(line)
    if not match:
        raise ValueError("联系人文件包含无效的 vCard 属性。")
    raw_header, raw = match.groups()
    header = raw_header.decode("utf-8")
    key, *params = re.split(r';(?=(?:[^"]*"[^"]*")*[^"]*$)', header)
    if not re.fullmatch(r"(?:[a-zA-Z0-9-]+\.)?[a-zA-Z0-9-]+", key):
        raise ValueError("联系人文件包含无效的 vCard 属性名称。")
    name = prop_name(key)
    charset = None
    kept = [key]
    for param in params:
        name_, assign, value = param.partition("=")
        param_name, value = name_.upper(), value.strip('"')
        if not assign:
            kept.append("TYPE=" + param)
        elif param_name == "CHARSET":
            charset = value
        elif param_name == "ENCODING" and value.upper() == "QUOTED-PRINTABLE":
            raw = quopri.decodestring(raw)
        elif param_name == "ENCODING" and value.upper() in ("BASE64", "B"):
            kept.append("ENCODING=b")
        else:
            kept.append(param)
    if charset:
        text = raw.decode(charset)
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            if version != "2.1":
                raise
            text = raw.decode("cp1252")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    if name == "BDAY" and re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if version == "4.0" and name == "PHOTO":
        inline = INLINE_PHOTO.fullmatch(text)
        if inline:
            kept = [key, "ENCODING=b", "TYPE=" + inline[1].upper()]
            text = inline[2]
        elif not any(part.upper().startswith("VALUE=") for part in kept[1:]):
            kept.append("VALUE=uri")
    if version == "4.0" and name == "TEL":
        kept = [part for part in kept if part.upper() not in ("VALUE=URI", "VALUE=TEXT")]
        if text.startswith("tel:"):
            text = unquote(text[4:]).replace("\r", "").replace("\n", "")
    return ";".join(kept), text


def escape(value, *, structured=False):
    value = value.replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\n", "\\n").replace(",", "\\,")
    return value if structured else value.replace(";", "\\;")


def display(value, *, structured=False):
    return re.sub(r"\\(.)", lambda m: "\n" if m[1].lower() == "n" else m[1],
                  value) if not structured else value


def values(card, name, *, visible=False):
    return [display(value, structured=name in STRUCTURED) if visible else value
            for header, value in card if prop_name(header) == name]


def uid(card):
    return next(iter(values(card, "UID", visible=True)), "")


def canonical(card):
    return sorted(card, key=lambda item: prop_name(item[0]))


def title(card):
    names = values(card, "FN", visible=True) or values(card, "N", visible=True)
    return names[0] if names and names[0] else uid(card)


def parse(data, *, source=None):
    cards, current = [], None
    for line in _lines(data):
        tag = line.upper()
        if tag == b"BEGIN:VCARD" and current is None:
            current = []
        elif tag == b"END:VCARD" and current is not None:
            versions = [part[8:].decode("ascii") for part in current
                        if part.upper().startswith(b"VERSION:")]
            if len(versions) != 1 or versions[0] not in ("2.1", "3.0", "4.0"):
                raise ValueError("联系人文件必须使用 vCard 2.1、3.0 或 4.0。")
            try:
                card = [_property(part, versions[0]) for part in current
                        if not part.upper().startswith(b"VERSION:")]
            except (UnicodeError, LookupError):
                raise ValueError("联系人文件包含无效的字符编码。") from None
            identifiers = values(card, "UID", visible=True)
            if len(identifiers) > 1:
                raise ValueError("联系人包含多个 UID。")
            if not uid(card):
                identity = fingerprint(source, len(cards)) if source else fingerprint(card)
                card = [(h, v) for h, v in card if prop_name(h) != "UID"]
                card.append(("UID", identity[:32]))
            if not values(card, "FN"):
                card.insert(0, ("FN", escape(title(card))))
            if not values(card, "N"):
                card.insert(1, ("N", ";;;;"))
            cards.append(card)
            current = None
        elif current is not None and tag != b"BEGIN:VCARD":
            current.append(line)
        elif line.strip() and not line.startswith((b"#", b";")):
            raise ValueError("联系人文件包含无效的 vCard 边界。")
    if current is not None or not cards:
        raise ValueError("联系人文件不包含完整 vCard。")
    return cards


def _fold(line):
    data = line.encode("utf-8")
    result = []
    start = 0
    limit = 75
    while len(data) - start > limit:
        end = start + limit
        while data[end] & 0xC0 == 0x80:
            end -= 1
        result.append(data[start:end])
        start, limit = end, 74
    return b"\r\n ".join([*result, data[start:]])


def serialize(cards):
    return b"".join(
        b"\r\n".join([b"BEGIN:VCARD", b"VERSION:3.0",
                      *(_fold(f"{header}:{value}") for header, value in card), b"END:VCARD", b""])
        for card in cards
    )


def revision(card):
    stamp = next(iter(values(card, "REV")), "")
    try:
        if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", stamp):
            value = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")
        else:
            value = datetime.fromisoformat(stamp.removesuffix("Z"))
        return value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp()
    except (ValueError, OverflowError):
        return float("-inf")


def edit(card, changes):
    """Keep untouched properties, including type labels, groups and photos."""
    result = list(card)

    def encode(name, value):
        if name in STRUCTURED:
            return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
        return escape(value)

    for name, edited in changes.items():
        entries = [i for i, (header, _) in enumerate(result) if prop_name(header) == name]
        if edited == values(result, name, visible=True):
            continue
        for index in reversed(range(len(entries))):
            i = entries[index]
            if index < len(edited):
                header, _ = result[i]
                result[i] = header, encode(name, edited[index])
            else:
                del result[i]
        result.extend((name, encode(name, value))
                      for value in edited[len(entries):])
    if result != list(card):
        result = [(h, v) for h, v in result if prop_name(h) != "REV"]
        result.append(("REV", datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
    return result


def new():
    return [("FN", ""), ("N", ";;;;"), ("UID", uuid4().hex)]


def records(data):
    """Yield byte offsets delimiting original vCards in a disk file."""
    active = None
    offset = 0
    for raw, line in _raw_lines(data):
        tag = line.removeprefix(b"\xef\xbb\xbf").upper()
        if tag == b"BEGIN:VCARD":
            if active is not None:
                raise ValueError("联系人文件包含无效的 vCard 边界。")
            active = offset + (3 if raw.startswith(b"\xef\xbb\xbf") else 0)
        offset += len(raw)
        if tag == b"END:VCARD" and active is not None:
            yield active, offset
            active = None
    if active is not None:
        raise ValueError("联系人文件不包含完整 vCard。")


def rewrite(raw, previous, card):
    """Preserve raw untouched lines in a vCard 3.0 or 4.0 source record."""
    lines = list(_raw_lines(raw))
    version = next(line[8:] for _, line in lines if line.upper().startswith(b"VERSION:"))
    actual_uid = any(prop_name(line.split(b":", 1)[0].decode("utf-8")) == "UID"
                     and line.partition(b":")[2] for _, line in lines)
    if canonical(previous) == canonical(card):
        if actual_uid:
            return raw
        return b"".join(original if line.upper() != b"END:VCARD"
                        else _fold("UID:" + escape(uid(card))) + b"\r\n" + original
                        for original, line in lines)
    if version == b"2.1":
        return serialize([card])
    def group(entry):
        result = {}
        for header, value in entry:
            result.setdefault(prop_name(header), []).append((header, value))
        return result

    original_fields, updated_fields = group(previous), group(card)
    changed = {name for name in original_fields.keys() | updated_fields.keys()
               if original_fields.get(name) != updated_fields.get(name)}
    if not actual_uid:
        changed.add("UID")
    out = []
    for original, line in lines:
        if line.upper() == b"END:VCARD":
            for header, value in card:
                name = prop_name(header)
                if name not in changed:
                    continue
                if version == b"4.0" and name == "TEL" and "VALUE=" not in header.upper():
                    header += ";VALUE=text"
                if version == b"4.0" and name == "PHOTO" and "ENCODING=B" in header.upper():
                    key, *params = header.split(";")
                    types = [p.partition("=")[2] for p in params if p.upper().startswith("TYPE=")]
                    subtype = types[0].casefold() if types else "jpeg"
                    header = key
                    value = f"data:image/{subtype};base64,{value}"
                if version == b"4.0" and name == "BDAY" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    value = value.replace("-", "")
                out.append(_fold(f"{header}:{value}") + b"\r\n")
        if prop_name(line.split(b":", 1)[0].decode("utf-8")) not in changed:
            out.append(original)
    return b"".join(out)
