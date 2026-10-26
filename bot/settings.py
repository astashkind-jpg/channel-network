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
    image_source: str = ""      # gigachat | free | generate; пусто — image_source из network.yaml
    image_category: str = ""    # категория Pixabay (transportation, food, animals…) — точнее поиск
    image_fallback: str = ""    # тематический запрос, если точной картинки не нашлось (например "car")
    stock_video: bool | None = None  # стоковые видео Pixabay; пусто — да для обычных каналов, нет для 18+
    video_query: str = ""       # запасной запрос для стокового видео; пусто — image_fallback

    @property
    def use_stock_video(self) -> bool:
        return self.cluster == "main" if self.stock_video is None else bool(self.stock_video)

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.chat_id)


@dataclass
class Network:
    name: str = "Сеть каналов"
    review_hours: float = 3
    default_model: str = "deepseek-v4.1-flash"
    fallback_models: list[str] = field(default_factory=lambda: ["qwen3.6-35b-a3b", "aliceai-llm"])
    image_source: str = "gigachat"      # gigachat (Kandinsky) | free (фотостоки) | generate (YandexART)
    art_model: str = "yandex-art/latest"
    image_aspect: str = "16:9"
    image_check: bool = True            # ИИ со зрением выбирает фото, где виден предмет поста
    image_check_model: str = "qwen3.6-35b-a3b"
    gen_attempts: int = 2               # сколько раз GigaChat перерисовывает, если ИИ забраковал картинку
    catalog_weekday: int = 6
    catalog_window: str = "12:00-13:00"
    promo_per_channel_per_day: int = 1
    video_max_age_days: int = 4
    video_max_mb: int = 49
    plan_budget_minutes: int = 35       # после стольких минут плана GigaChat не рисует — фото со стоков
    stock_video: bool = True            # видео-рубрики: брать бесплатные ролики Pixabay, если нет других
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
    pexels_key: str = ""
    gigachat_key: str = ""
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
            pexels_key=g("PEXELS_KEY", ""),
            gigachat_key=g("GIGACHAT_AUTH_KEY", ""),
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


def normalize_chat_id(value) -> str:
    """Понимает любой привычный вид: @name, name, t.me/name, https://t.me/name, -100123…"""
    v = str(value or "").strip().strip('"').strip("'")
    if not v:
        return ""
    if v.lstrip("-").isdigit():
        return v
    for prefix in ("https://", "http://"):
        if v.startswith(prefix):
            v = v[len(prefix):]
    if v.startswith("t.me/") or v.startswith("telegram.me/"):
        v = v.split("/", 1)[1]
    if v.startswith("+") or v.startswith("joinchat"):
        return v  # пригласительная ссылка — не адрес канала; /check покажет ошибку
    v = v.split("/")[0].split("?")[0]
    return v if v.startswith("@") else f"@{v}"


def load_channels(config_dir: Path | None = None) -> list[Channel]:
    data = _load_yaml("channels.yaml", config_dir)
    channels = []
    for raw in data.get("channels", []):
        raw = dict(raw)
        rubrics = [Rubric(**r) for r in raw.pop("rubrics", [])]
        raw["chat_id"] = normalize_chat_id(raw.get("chat_id"))
        channels.append(Channel(rubrics=rubrics, **raw))
    keys = [c.key for c in channels]
    if len(keys) != len(set(keys)):
        raise ValueError("ключи каналов в channels.yaml повторяются")
    return channels
