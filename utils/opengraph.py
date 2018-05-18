from urllib.parse import urlparse

import ipaddress
import opengraph
import requests
from bs4 import BeautifulSoup

from .urlutils import is_url_valid


def links_from_note(note):
    tags_href= set()
    for t in note.get('tag', []):
        h = t.get('href')
        if h:
            # TODO(tsileo): fetch the URL for Actor profile, type=mention
            tags_href.add(h)

    links = set()
    soup = BeautifulSoup(note['content'])
    for link in soup.find_all('a'):
        h = link.get('href')
        if h.startswith('http') and h not in tags_href and is_url_valid(h):
            links.add(h)

    return links


def fetch_og_metadata(user_agent, col, remote_id):
    doc = col.find_one({'remote_id': remote_id})
    if not doc:
        raise ValueError
    note = doc['activity']['object']
    print(note)
    links = links_from_note(note)
    if not links:
        return 0
    # FIXME(tsileo): set the user agent by giving HTML directly to OpenGraph
    htmls = []
    for l in links:
        r = requests.get(l, headers={'User-Agent': user_agent})
        r.raise_for_status()
        htmls.append(r.text)
    links_og_metadata = [dict(opengraph.OpenGraph(html=html)) for html in htmls]
    col.update_one({'remote_id': remote_id}, {'$set': {'meta.og_metadata': links_og_metadata}})
    return len(links)
