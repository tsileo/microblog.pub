import typing                                       
import re                                           

from bleach.linkifier import Linker                 
from markdown import markdown                       

from utils.webfinger import get_actor_url           
from config import USERNAME, BASE_URL, ID           
from config import ACTOR_SERVICE       

from typing import List, Optional, Tuple, Dict, Any, Union, Type                                         


def set_attrs(attrs, new=False):                    
    attrs[(None, u'target')] = u'_blank'            
    attrs[(None, u'class')] = u'external'           
    attrs[(None, u'rel')] = u'noopener'             
    attrs[(None, u'title')] = attrs[(None, u'href')]
    return attrs                                    


LINKER = Linker(callbacks=[set_attrs])
HASHTAG_REGEX = re.compile(r"(#[\d\w\.]+)")
MENTION_REGEX = re.compile(r"@[\d\w_.+-]+@[\d\w-]+\.[\d\w\-.]+")


def hashtagify(content: str) -> Tuple[str, List[Dict[str, str]]]:
    tags = []
    for hashtag in re.findall(HASHTAG_REGEX, content):
        tag = hashtag[1:]
        link = f'<a href="{BASE_URL}/tags/{tag}" class="mention hashtag" rel="tag">#<span>{tag}</span></a>'
        tags.append(dict(href=f'{BASE_URL}/tags/{tag}', name=hashtag, type='Hashtag'))
        content = content.replace(hashtag, link)
    return content, tags


def mentionify(content: str) -> Tuple[str, List[Dict[str, str]]]:
    tags = []
    for mention in re.findall(MENTION_REGEX, content):
        _, username, domain = mention.split('@')
        actor_url = get_actor_url(mention)
        p = ACTOR_SERVICE.get(actor_url)
        print(p)
        tags.append(dict(type='Mention', href=p['id'], name=mention))
        link = f'<span class="h-card"><a href="{p["url"]}" class="u-url mention">@<span>{username}</span></a></span>'
        content = content.replace(mention, link)
    return content, tags


def parse_markdown(content: str) -> Tuple[str, List[Dict[str, str]]]:
    tags = []
    content = LINKER.linkify(content)
    content, hashtag_tags = hashtagify(content)
    tags.extend(hashtag_tags)
    content, mention_tags = mentionify(content)
    tags.extend(mention_tags)
    content = markdown(content)
    return content, tags
