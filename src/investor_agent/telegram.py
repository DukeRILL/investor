"""Telegram Bot API and Telegraph clients. Tokens never appear in logs or errors."""

import json
import logging

from .http import Http, HttpError

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    """ambiguous=True: the request may have been executed (timeout, connection lost, 5xx) - the caller must not
    assume 'not delivered'. A Telegram answer ok=false or HTTP 4xx is a definite failure."""

    def __init__(self, message: str = "", ambiguous: bool = False):
        super().__init__(message)
        self.ambiguous = ambiguous


class TelegramClient:
    def __init__(self, http: Http, token: str):
        self.http, self._token = http, token

    def _call(self, method: str, payload: dict) -> dict:
        if not self._token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not configured")
        try:
            data = self.http.request("POST", f"https://api.telegram.org/bot{self._token}/{method}",
                                     json_body=payload, timeout=60)
        except HttpError as exc:  # strip URL with token from the message
            raise TelegramError(f"{method} failed: HTTP {exc.status}", ambiguous=exc.status >= 500) from None
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, (OSError, TimeoutError)) or type(exc).__module__.startswith("httpx"):
                raise TelegramError(f"{method} failed: {type(exc).__name__}", ambiguous=True) from None
            raise
        if not isinstance(data, dict) or not data.get("ok"):
            desc = data.get("description") if isinstance(data, dict) else "bad response"
            raise TelegramError(f"{method} failed: {desc}")
        return data["result"]

    def send(self, chat_id: str, html: str, button: tuple[str, str] | None = None,
             disable_preview: bool = True, reply_to: int | None = None) -> int:
        payload = {"chat_id": chat_id, "text": html, "parse_mode": "HTML",
                   "link_preview_options": {"is_disabled": disable_preview}}
        if reply_to:  # Telegram shows the quoted earlier post above the new one
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        if button:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": button[0], "url": button[1]}]]}
        return int(self._call("sendMessage", payload)["message_id"])

    def send_photo(self, chat_id: str, photo_url: str, html: str, button: tuple[str, str] | None = None) -> int:
        """Photo by public URL with an HTML caption (max 1024 visible characters)."""
        payload = {"chat_id": chat_id, "photo": photo_url, "caption": html, "parse_mode": "HTML"}
        if button:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": button[0], "url": button[1]}]]}
        return int(self._call("sendPhoto", payload)["message_id"])

    def edit_caption(self, chat_id: str, message_id: int, html: str, button: tuple[str, str] | None = None) -> None:
        """Change the caption of a photo post. The button must be passed again or Telegram removes it."""
        payload = {"chat_id": chat_id, "message_id": message_id, "caption": html, "parse_mode": "HTML"}
        if button:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": button[0], "url": button[1]}]]}
        self._call("editMessageCaption", payload)

    def edit_text(self, chat_id: str, message_id: int, html: str, button: tuple[str, str] | None = None) -> None:
        """Change the text of a text post. The button must be passed again or Telegram removes it."""
        payload = {"chat_id": chat_id, "message_id": message_id, "text": html, "parse_mode": "HTML",
                   "link_preview_options": {"is_disabled": True}}
        if button:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": button[0], "url": button[1]}]]}
        self._call("editMessageText", payload)

    def pin(self, chat_id: str, message_id: int) -> bool:
        """Pin silently (the bot needs the 'pin messages' right in the channel)."""
        return bool(self._call("pinChatMessage", {"chat_id": chat_id, "message_id": message_id,
                                                  "disable_notification": True}))

    def send_poll(self, chat_id: str, question: str, options: list[str]) -> int:
        """Anonymous regular poll (channels allow only anonymous polls)."""
        payload = {"chat_id": chat_id, "question": question, "options": [{"text": o} for o in options],
                   "is_anonymous": True, "allows_multiple_answers": False}
        return int(self._call("sendPoll", payload)["message_id"])

    def stop_poll(self, chat_id: str, message_id: int) -> dict:
        """Close a poll sent by the bot; returns the final Poll with voter_count per option."""
        return self._call("stopPoll", {"chat_id": chat_id, "message_id": message_id})

    def set_buttons(self, chat_id: str, message_id: int, rows: list[list[tuple[str, str]]]) -> None:
        """Replace the inline keyboard of a post (rows of (text, url))."""
        keyboard = [[{"text": t, "url": u} for t, u in row] for row in rows]
        self._call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                              "reply_markup": {"inline_keyboard": keyboard}})

    def create_invite_link(self, chat_id: str, name: str) -> str:
        """A separate invite link (its own counter and chat_member.invite_link) for one traffic source."""
        return self._call("createChatInviteLink", {"chat_id": chat_id, "name": name[:32]})["invite_link"]

    def member_count(self, chat_id: str) -> int:
        return int(self._call("getChatMemberCount", {"chat_id": chat_id}))

    def webhook_url(self) -> str:
        return (self._call("getWebhookInfo", {}) or {}).get("url", "")

    def get_updates(self, offset: int | None, allowed: list[str], limit: int = 100) -> list[dict]:
        payload = {"timeout": 0, "limit": limit, "allowed_updates": allowed}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload)

    def delete(self, chat_id: str, message_id: int) -> bool:
        return bool(self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}))

    def send_series(self, chat_id: str, parts: list[str], button: tuple[str, str] | None = None,
                    reply_to: int | None = None) -> list[int]:
        """Send all parts; if one fails, delete the ones already sent so no fragment stays."""
        sent: list[int] = []
        try:
            for i, part in enumerate(parts):
                sent.append(self.send(chat_id, part, button if i == len(parts) - 1 else None,
                                      reply_to=reply_to if i == 0 else None))
        except TelegramError:
            for mid in sent:
                try:
                    self.delete(chat_id, mid)
                except TelegramError:
                    log.warning("could not delete partial message")
            raise
        return sent


class TelegraphClient:
    API = "https://api.telegra.ph"

    def __init__(self, http: Http, token_getter, token_setter, short_name: str = "investor_duker",
                 author_name: str = "Школа единорогов", author_url: str = "https://t.me/investor_duker"):
        self.http = http
        self._get, self._set = token_getter, token_setter
        self.short_name, self.author_name, self.author_url = short_name, author_name, author_url

    def _call(self, method: str, payload: dict) -> dict:
        data = self.http.request("POST", f"{self.API}/{method}", json_body=payload, timeout=60)
        if not isinstance(data, dict) or not data.get("ok"):
            raise TelegramError(f"telegraph {method} failed: {data.get('error') if isinstance(data, dict) else data}")
        return data["result"]

    def _token(self) -> str:
        token = self._get()
        if not token:
            result = self._call("createAccount", {"short_name": self.short_name, "author_name": self.author_name,
                                                  "author_url": self.author_url})
            token = result["access_token"]
            self._set(token)
        return token

    def upload_image(self, data: bytes, content_type: str) -> str:
        """Upload a jpeg/png/gif to telegra.ph and return its absolute URL."""
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif"}[content_type]
        result = self.http.post_file("https://telegra.ph/upload", f"image.{ext}", data, content_type)
        if not isinstance(result, list) or not result or not result[0].get("src"):
            raise TelegramError(f"telegraph upload failed: {str(result)[:120]}")
        return "https://telegra.ph" + result[0]["src"]

    def edit_page(self, url: str, title: str, nodes: list) -> str:
        """Replace the content of a page this account created (the pinned glossary grows this way)."""
        payload = {"access_token": self._token(), "path": url.rstrip("/").rsplit("/", 1)[-1], "title": title[:256],
                   "author_name": self.author_name, "author_url": self.author_url,
                   "content": json.dumps(nodes, ensure_ascii=False, separators=(",", ":")), "return_content": False}
        return self._call("editPage", payload)["url"]

    def find_page(self, title: str) -> str | None:
        """URL of the page of this account with exactly this title (recovers a page created just before a crash).
        None = looked and there is none. Several pages with that title (or a failed lookup) raise: the caller must
        not create another page then."""
        result = self._call("getPageList", {"access_token": self._token(), "limit": 200})
        found = [p["url"] for p in result.get("pages", []) if p.get("title") == title[:256]]
        if len(found) > 1:
            raise TelegramError(f"telegraph: {len(found)} pages share the title, reconcile by hand")
        return found[0] if found else None

    def get_page(self, url: str) -> dict:
        """Current title and content nodes of a page (used to append blocks without recreating it)."""
        path = url.rstrip("/").rsplit("/", 1)[-1]
        return self._call(f"getPage/{path}", {"return_content": True})

    def get_views(self, url: str) -> int:
        """Total views of a page (official getViews; year/month/day/hour narrow the interval, unused here)."""
        path = url.rstrip("/").rsplit("/", 1)[-1]
        return int(self._call(f"getViews/{path}", {})["views"])

    def create_page(self, title: str, nodes: list) -> str:
        payload = {"access_token": self._token(), "title": title[:256], "author_name": self.author_name,
                   "author_url": self.author_url, "content": json.dumps(nodes, ensure_ascii=False, separators=(",", ":")),  # Telegraph caps content at 64 KB
                   "return_content": False}
        return self._call("createPage", payload)["url"]
