"""Загрузка настроек: config/*.yaml + секреты из переменных окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


@dataclass
class Rubric:
    name: str
    type: str = "text"          # text | video | catalog
    weight: float = 1.0
    prompt: str = ""
    image: bool = True          # подбирать картинку к текстовому посту
    slots: list[int] | None = None  # номера окон (с 0), в которые можно ставить рубрику; None — в любые


@dataclass
class Channel:
    key: str
    title: str
    enabled: bool = False
    chat_id: str = ""           # @username или числовой id
    invite_link: str = ""       # ссылка для кросс-пиара
    tagline: str = ""           # одна строка о канале для кросс-пиара
    emoji: str = "📢"
    cluster: str = "main"       # main | adult — кросс-пиар только внутри кластера
    model: str = ""             # модель Yandex AI Studio; пусто — default_model из network.yaml
    auto_approve: bool = True   # публиковать без ✅, если вы не ответили до дедлайна
    windows: list[str] = field(default_factory=list)
    persona: str = ""
    rubrics: list[Rubric] = field(default_factory=list)
    video_sources: list[str] = field(default_factory=list)
    signature: str = ""         # строка в конце каждого поста (например, ❤️)

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.chat_id)


@dataclass
class Network:
    name: str = "Сеть каналов"
    review_hours: float = 3
    default_model: str = "deepseek-v4.1-flash"
    catalog_weekday: int = 6
    catalog_window: str = "12:00-13:00"
    promo_per_channel_per_day: int = 1
    video_max_age_days: int = 4
    video_max_mb: int = 49
    recent_topics: int = 60
    ad_markers: list[str] = field(default_factory=lambda: ["erid", "реклама", "#ad", "промокод"])


@dataclass
class Secrets:
    bot_token: str = ""
    admin_chat_id: str = ""
    storage_chat_id: str = ""
    yandex_api_key: str = ""
    yandex_folder_id: str = ""
    pixabay_key: str = ""
    tg_api_id: str = ""
    tg_api_hash: str = ""
    tg_session: str = ""

    @classmethod
    def from_env(cls) -> "Secrets":
        g = os.environ.get
        return cls(
            bot_token=g("TG_BOT_TOKEN", ""),
            admin_chat_id=g("ADMIN_CHAT_ID", ""),
            storage_chat_id=g("STORAGE_CHAT_ID", ""),
            yandex_api_key=g("YANDEX_API_KEY", ""),
            yandex_folder_id=g("YANDEX_FOLDER_ID", ""),
            pixabay_key=g("PIXABAY_KEY", ""),
            tg_api_id=g("TG_API_ID", ""),
            tg_api_hash=g("TG_API_HASH", ""),
            tg_session=g("TG_SESSION", ""),
        )


def _load_yaml(name: str, config_dir: Path | None = None) -> dict:
    path = (config_dir or CONFIG_DIR) / name
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_network(config_dir: Path | None = None) -> Network:
    data = _load_yaml("network.yaml", config_dir)
    known = Network.__dataclass_fields__.keys()
    return Network(**{k: v for k, v in data.items() if k in known})


def load_channels(config_dir: Path | None = None) -> list[Channel]:
    data = _load_yaml("channels.yaml", config_dir)
    channels = []
    for raw in data.get("channels", []):
        raw = dict(raw)
        rubrics = [Rubric(**r) for r in raw.pop("rubrics", [])]
        raw["chat_id"] = str(raw.get("chat_id") or "")
        channels.append(Channel(rubrics=rubrics, **raw))
    keys = [c.key for c in channels]
    if len(keys) != len(set(keys)):
        raise ValueError("ключи каналов в channels.yaml повторяются")
    return channels
