"""CalDAV discovery and a conditional, incremental collection snapshot."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import http.client
import re
from urllib.parse import quote, unquote, urljoin, urlsplit
from xml.etree import ElementTree as XML

DAV = "{DAV:}"
CAL = "{urn:ietf:params:xml:ns:caldav}"
CTAG = "{http://calendarserver.org/ns/}getctag"
KINDS = ("VEVENT", "VTODO")
MAX_RESPONSE = 32 * 1024 * 1024
STRONG_ETAG = re.compile(r'"[\x21\x23-\x7e\x80-\xff]*"')


@dataclass(frozen=True)
class Collection:
    url: str
    name: str
    kinds: tuple[str, ...]


class DavError(RuntimeError):
    def __init__(self, method, status):
        self.status = status
        super().__init__(f"CalDAV {method} 返回 HTTP {status}。")


def origin(url):
    parts = urlsplit(url)
    if (not parts.hostname or parts.username is not None or parts.password is not None
            or parts.fragment or any(ord(char) < 33 for char in url)
            or (parts.scheme != "https" and not (
                parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}))):
        raise ValueError("CalDAV 必须使用 HTTPS。仅本机测试可以使用 HTTP。")
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def _member(collection, href):
    parent = urlsplit(collection).path.rstrip("/")
    target = urlsplit(href).path
    if not target.startswith(parent + "/"):
        raise ValueError("CalDAV 日历报告包含集合之外的资源。")
    child = unquote(target[len(parent) + 1:].rstrip("/"))
    if not child or child in (".", "..") or "/" in child:
        raise ValueError("CalDAV 日历报告包含无效的成员地址。")


def _status(text):
    try:
        return int(text.split()[1])
    except (IndexError, ValueError):
        return 0


class Client:
    def __init__(self, url, username, password):
        self.origin = origin(url)
        self.url = url
        if not username or ":" in username:
            raise ValueError("请输入有效的 CalDAV 用户名。")
        self.auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()

    def resolve(self, base, href):
        url = urljoin(base, href)
        if origin(url) != self.origin:
            raise ValueError("CalDAV 重定向到其他主机。请直接输入目标服务器地址。")
        return url

    def request(self, method, url, body=None, *, headers=None, expected=(207,), redirects=0):
        if origin(url) != self.origin:
            raise ValueError("CalDAV 请求的服务器地址已更改。")
        scheme, host, port = self.origin
        conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(host, port, timeout=20)
        parts = urlsplit(url)
        target = quote(parts.path or "/", safe="/%!$&'()*+,;=:@")
        if parts.query:
            target += "?" + quote(parts.query, safe="/?%!$&'()*+,;=:@")
        request_headers = {"Authorization": self.auth, **(headers or {})}
        try:
            conn.request(method, target, body, request_headers)
            response = conn.getresponse()
            status, reply = response.status, response.headers
            if status in (301, 302, 307, 308) and redirects and reply.get("Location"):
                location = self.resolve(url, reply["Location"])
            else:
                location = None
                if status not in expected:
                    raise DavError(method, status)
                data = response.read(MAX_RESPONSE + 1)
                if len(data) > MAX_RESPONSE:
                    raise ValueError("CalDAV 响应超过 32 MiB。")
        except (http.client.HTTPException, OSError):
            raise RuntimeError("CalDAV 网络连接失败。请检查地址、网络和证书。") from None
        finally:
            conn.close()
        if location is not None:
            return self.request(method, location, body, headers=headers,
                                expected=expected, redirects=redirects - 1)
        return url, reply, data

    def xml(self, method, url, root, depth, *, redirects=0):
        url, _, data = self.request(
            method, url, XML.tostring(root, encoding="utf-8"),
            headers={"Content-Type": "application/xml; charset=utf-8", "Depth": str(depth)},
            redirects=redirects,
        )
        # DAV has no use for DTDs. Parse UTF-8 only to make the check encoding-independent.
        try:
            source = data.decode("utf-8-sig")
            if "<!DOCTYPE" in source or "<!ENTITY" in source:
                raise ValueError
            tree = XML.fromstring(source)
        except (UnicodeError, XML.ParseError, ValueError):
            raise ValueError("CalDAV 返回了无效的 XML。") from None
        if tree.tag != DAV + "multistatus":
            raise ValueError("CalDAV 返回了无效的多状态响应。")
        return url, tree

    def properties(self, url, depth, *names):
        root = XML.Element(DAV + "propfind")
        prop = XML.SubElement(root, DAV + "prop")
        for name in names:
            XML.SubElement(prop, name)
        final, tree = self.xml("PROPFIND", url, root, depth, redirects=5)
        return final, list(self.responses(final, tree))

    def responses(self, base, tree):
        for response in tree.findall(DAV + "response"):
            href = response.findtext(DAV + "href")
            if not href:
                raise ValueError("CalDAV 响应缺少资源地址。")
            properties = {}
            status = _status(response.findtext(DAV + "status", ""))
            for propstat in response.findall(DAV + "propstat"):
                if 200 <= _status(propstat.findtext(DAV + "status", "")) < 300:
                    properties.update({node.tag: node for node in propstat.findall(DAV + "prop/*")})
            if status and status not in (200, 404, 410):
                raise DavError("multistatus", status)
            yield self.resolve(base, href), status, properties

    def discover(self):
        well_known = self.resolve(self.url, "/.well-known/caldav")
        for start in dict.fromkeys((self.url, well_known)):
            try:
                final, rows = self.properties(start, 0, DAV + "current-user-principal",
                                              CAL + "calendar-home-set", DAV + "resourcetype")
            except DavError as error:
                if start != well_known and error.status in (400, 403, 404, 405, 501):
                    continue
                raise
            for href, _, properties in rows:
                homes = self._homes(href, properties)
                principal = properties.get(DAV + "current-user-principal")
                if not homes and principal is not None:
                    principal_href = principal.findtext(DAV + "href")
                    if principal_href:
                        _, principals = self.properties(self.resolve(href, principal_href), 0,
                                                        CAL + "calendar-home-set")
                        for principal_url, _, props in principals:
                            homes.extend(self._homes(principal_url, props))
                if homes:
                    return self._collections(homes)
                kind = properties.get(DAV + "resourcetype")
                if kind is not None and kind.find(CAL + "calendar") is not None:
                    return self._collections([href], depth=0)
            # A server URL can also be the calendar home itself.
            try:
                calendars = self._collections([final])
            except DavError as error:
                if start == well_known or error.status not in (400, 403, 404, 405, 501):
                    raise
                continue
            if calendars:
                return calendars
        return []

    def _homes(self, url, properties):
        home = properties.get(CAL + "calendar-home-set")
        return [] if home is None else [self.resolve(url, node.text)
                                        for node in home.findall(DAV + "href") if node.text]

    def _collections(self, homes, depth=1):
        result = {}
        for home in dict.fromkeys(homes):
            _, rows = self.properties(home, depth, DAV + "resourcetype", DAV + "displayname",
                                      CAL + "supported-calendar-component-set")
            for href, _, properties in rows:
                kind = properties.get(DAV + "resourcetype")
                if kind is None or kind.find(CAL + "calendar") is None:
                    continue
                components = properties.get(CAL + "supported-calendar-component-set")
                supported = KINDS if components is None else tuple(
                    name for name in KINDS
                    if any(node.get("name") == name for node in components))
                if supported:
                    name = properties.get(DAV + "displayname")
                    result[href] = Collection(href, (name.text if name is not None else None)
                                              or urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1],
                                              supported)
        return sorted(result.values(), key=lambda collection: (collection.name.casefold(), collection.url))

    def snapshot(self, url, cached):
        """Keep the token from before listing, so a concurrent change is replayed."""
        _, rows = self.properties(url, 0, DAV + "sync-token", CTAG)
        props = rows[0][2] if rows else {}
        token = props[DAV + "sync-token"].text if DAV + "sync-token" in props else None
        ctag = props[CTAG].text if CTAG in props else None
        old = cached.get("resources", {})
        previous = cached.get("token")
        if previous and token:
            if previous == token:
                return cached
            try:
                return self._delta(url, previous, ctag, old)
            except DavError as error:
                if error.status not in (400, 403, 405, 409, 501, 507):
                    raise
        elif ctag and ctag == cached.get("ctag"):
            return cached
        final, rows = self.properties(url, 1, DAV + "getetag", DAV + "resourcetype")
        resources = {}
        for href, status, props in rows:
            kind = props.get(DAV + "resourcetype")
            if (href.rstrip("/") == final.rstrip("/") or status in (404, 410)
                    or (kind is not None and kind.find(DAV + "collection") is not None)):
                continue
            _member(final, href)
            resources[href] = self._fetch_changed(href, props, old)
        return dict(token=token, ctag=ctag, resources=resources)

    def _delta(self, url, previous, ctag, old):
        resources = old.copy()
        visited = set()
        while previous not in visited:
            visited.add(previous)
            root = XML.Element(DAV + "sync-collection")
            XML.SubElement(root, DAV + "sync-token").text = previous
            XML.SubElement(root, DAV + "sync-level").text = "1"
            XML.SubElement(XML.SubElement(root, DAV + "prop"), DAV + "getetag")
            final, tree = self.xml("REPORT", url, root, 0)
            token = tree.findtext(DAV + "sync-token")
            truncated = False
            for response in tree.findall(DAV + "response"):
                if (_status(response.findtext(DAV + "status", "")) == 507
                        and self.resolve(final, response.findtext(DAV + "href", "")).rstrip("/")
                        == final.rstrip("/")):
                    truncated = True
                    tree.remove(response)
            for href, status, props in self.responses(final, tree):
                if href.rstrip("/") == final.rstrip("/"):
                    continue
                _member(final, href)
                if status in (404, 410):
                    resources.pop(href, None)
                else:
                    resources[href] = self._fetch_changed(href, props, resources)
            if not token:
                raise ValueError("CalDAV 同步响应缺少有效的同步令牌。")
            if not truncated:
                return dict(token=token, ctag=ctag, resources=resources)
            previous = token
        raise ValueError("CalDAV 返回了重复的同步令牌。")

    def _fetch_changed(self, href, properties, cached):
        etag = properties.get(DAV + "getetag")
        old = cached.get(href)
        if old and etag is not None and etag.text and old["etag"] == etag.text:
            return old
        return self.get(href)

    def get(self, href):
        _, headers, body = self.request("GET", href, expected=(200,))
        return {"etag": headers.get("ETag"), "body": body.decode("utf-8-sig")}

    def put(self, href, body, *, etag=None, new=False):
        if not new and (not etag or not STRONG_ETAG.fullmatch(etag)):
            raise ValueError("服务器未提供强 ETag，无法安全写回日历。")
        condition = {"If-None-Match": "*"} if new else {"If-Match": etag}
        _, headers, _ = self.request(
            "PUT", href, body.encode(),
            headers={**condition, "Content-Type": "text/calendar; charset=utf-8"},
            expected=(200, 201, 204),
        )
        written_etag = headers.get("ETag", "")
        return written_etag if STRONG_ETAG.fullmatch(written_etag) else None

    def delete(self, href, etag):
        if not etag or not STRONG_ETAG.fullmatch(etag):
            raise ValueError("服务器未提供强 ETag，无法安全删除日历条目。")
        self.request("DELETE", href, headers={"If-Match": etag}, expected=(200, 204))
