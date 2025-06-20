class TrackNotFound(Exception):
    pass


class LyricsAlreadyExists(Exception):
    pass


class LyricsNotAvailable(Exception):
    pass


class UnsupportedExtension(Exception):
    pass


class NotEnoughMetadata(Exception):
    pass


class RequestFailed(Exception):
    pass