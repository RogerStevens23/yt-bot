import re

YOUTUBE_REGEX = re.compile(
    r'(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[^\s]+)'
)


class Regex:

    def __init__(self):
        self

    @classmethod
    def search_for_youtube_link(cls, text):
        matches = YOUTUBE_REGEX.findall(text)
        return matches