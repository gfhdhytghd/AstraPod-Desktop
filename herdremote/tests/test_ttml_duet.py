"""TTML voice metadata through both mock providers and native CL framing."""
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from hrcider import native_page_frame
from hrcider_library import Library
from hrrufin import RufinLibrary, rufin_id


TTML = """<tt xmlns="http://www.w3.org/ns/ttml"
 xmlns:ttm="http://www.w3.org/ns/ttml#metadata">
 <head><metadata>
  <ttm:agent type="person" xml:id="two"><ttm:name type="full">Sage</ttm:name></ttm:agent>
  <ttm:agent type="person" xml:id="one"/>
  <ttm:agent type="group" xml:id="all"/>
  <ttm:agent type="person" xml:id="three"/>
  <ttm:agent type="other" xml:id="guest"/>
  <ttm:agent type="group" xml:id="second_group"/>
 </metadata></head>
 <body>
  <div ttm:agent="one"><p begin="0s" end="3s">
   <span begin="1s" end="2s">Alpha &amp; </span><span begin="2s" end="3s">light</span>
   <span ttm:role="x-bg" ttm:agent="two"><span begin="0.5s" end="1.5s">echo</span></span>
  </p></div>
  <div>
   <p begin="3s" end="4s" ttm:agent="two">Beta</p>
   <p begin="4s" end="5s" ttm:agent="all">Chorus</p>
   <p begin="5s" end="6s" ttm:agent="one two">Together</p>
   <p begin="6s" end="7s"><span begin="6s" end="6.4s" ttm:agent="two">Right</span>
      <span begin="6.4s" end="7s" ttm:agent="one"> Left</span></p>
   <p begin="7s" end="8s" ttm:agent="three">Third</p>
   <p begin="8s" end="9s" ttm:agent="guest">Guest</p>
   <p begin="9s" end="10s" ttm:agent="second_group">Second group</p>
  </div>
 </body>
</tt>"""


class Cider:
    def __init__(self, ttml):
        self.ttml = ttml
        self.calls = 0

    def now_playing(self):
        return {'id': '123', 'durationInMillis': 12000}

    def request(self, path):
        assert path == '/api/v1/lyrics/123'
        self.calls += 1
        return {'data': [{'attributes': {'ttml': self.ttml}}]}


class Rufin:
    uri = 'local:track/duet'

    def __init__(self, ttml):
        self.ttml = ttml

    def playback(self):
        return {'duration_ms': 12000, 'current': {'media_uri': self.uri, 'run': 3}}

    def get(self, path):
        assert path == '/api/lyrics'
        return {'state': 'ready', 'media_uri': self.uri, 'run': 3,
                'document': {'ttml': self.ttml}}


def get(backend, ttml):
    if backend == 'cider':
        return Library(SimpleNamespace(generation=1), Cider(ttml)), '123'
    return RufinLibrary(Rufin(ttml)), rufin_id(Rufin.uri)


def decode(page):
    wire = native_page_frame(5, 8, page).decode().splitlines()[1:]
    packets, parts = [], []
    for raw in wire:
        kind, title, count, *_ = raw.split('\t')
        assert kind in ('ttml2', 'ttml2+')
        parts.append(title)
        if kind == 'ttml2':
            packets.append((int(count), ET.fromstring(''.join(parts))))
            parts.clear()
    assert not parts
    return packets


@pytest.mark.parametrize('backend', ['cider', 'rufin'])
def test_performers_background_and_legacy(backend):
    library, track = get(backend, TTML)
    legacy = f'now/lyrics?nonce=1&lrc=1&track={track}'
    query = legacy + '&duet=1'
    assert library.lyrics.select(track)
    assert library.lyrics.prefetch(track, library.timed_lyrics)
    page = library.page(query, 0, 50)
    assert page['total'] == len(page['items'])
    decoded = decode(page)
    assert [count for count, _ in decoded] == [6] * 8
    assert page['items'][1:3] == library.page(query, 1, 2)['items']
    lines = [line for _, line in decoded]
    assert [line.get('a') for line in lines] == ['3', '4', '256', '1', '0', '5', '512', '257']
    assert lines[0][0].text == 'Alpha & '
    bg = lines[0].find("./span[@bg='1']")
    assert bg.get('a') == '4'
    assert (bg[0].get('begin'), bg[0].get('end'), bg[0].text) == ('500ms', '1500ms', 'echo')
    assert [(span.get('a'), span.get('begin')) for span in lines[4]] == [('4', '6000ms'), ('3', '6400ms')]
    old = library.page(legacy, 0, 50)
    assert all(item['id'] in ('lrc1', 'lrc1+') for item in old['items'])
    assert all(item['subtitle'] == '' for item in old['items'])
    if backend == 'cider':
        assert library.client.calls == 1


@pytest.mark.parametrize('backend', ['cider', 'rufin'])
def test_no_agents_keeps_timed_legacy_format(backend):
    library, track = get(backend, '<tt><body><div><p begin="1s">Solo</p></div></body></tt>')
    query = f'now/lyrics?nonce=1&lrc=1&duet=1&track={track}'
    item, = library.page(query, 0, 50)['items']
    assert (item['id'], item['title']) == ('lrc1', '[00:01.00]Solo')
