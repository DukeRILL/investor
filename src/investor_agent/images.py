"""Image hosting for the Unicorn School page. Telegraph's own upload no longer works (HTTP 400), so images go to
Cloudinary (free plan, signed upload; keys live only in .env). The file name is a hash of the content, so the same
picture is never stored twice."""

import hashlib
import time

from .http import Http

EXT = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif"}


class ImageHostError(RuntimeError):
    pass


class CloudinaryClient:
    def __init__(self, http: Http, cloud_name: str, api_key: str, api_secret: str, folder: str = "unicorn"):
        self.http, self.cloud, self.key, self._secret, self.folder = http, cloud_name, api_key, api_secret, folder

    def upload_image(self, data: bytes, content_type: str) -> str:
        """Upload jpeg/png/gif bytes; return the permanent https URL."""
        public_id = f"{self.folder}/{hashlib.sha1(data).hexdigest()[:20]}"
        timestamp = str(int(time.time()))
        signature = hashlib.sha1(f"public_id={public_id}&timestamp={timestamp}{self._secret}".encode()).hexdigest()
        result = self.http.post_file(f"https://api.cloudinary.com/v1_1/{self.cloud}/image/upload",
                                     f"image.{EXT[content_type]}", data, content_type,
                                     fields={"api_key": self.key, "timestamp": timestamp, "public_id": public_id,
                                             "signature": signature})
        url = result.get("secure_url") if isinstance(result, dict) else None
        if not url:
            raise ImageHostError("image host returned no URL")  # never include the response: it may echo parameters
        return url
