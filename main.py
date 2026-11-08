"""TG Music — плеер, который берёт музыку из вашего Telegram-канала.

Первый запуск: приложение спросит api_id, api_hash (https://my.telegram.org),
канал и войдёт в ваш аккаунт (телефон -> код -> пароль 2FA, если включён).
Треки скачиваются в кэш при первом проигрывании и дальше играют офлайн.
"""
import asyncio
import json
import os
import shutil

from kivy.app import App
from kivy.clock import Clock
from kivy.core.audio import SoundLoader
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.popup import Popup
from kivy.uix.textinput import TextInput
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import DocumentAttributeAudio, InputMessagesFilterMusic

KV = """
BoxLayout:
    orientation: 'vertical'
    padding: dp(8)
    spacing: dp(6)

    BoxLayout:
        size_hint_y: None
        height: dp(44)
        spacing: dp(6)
        Label:
            id: status
            text: 'Запуск...'
            text_size: self.size
            halign: 'left'
            valign: 'middle'
            shorten: True
        Button:
            text: 'Обновить'
            size_hint_x: None
            width: dp(100)
            on_release: app.refresh()
        Button:
            text: 'Сброс'
            size_hint_x: None
            width: dp(80)
            on_release: app.reset()

    ScrollView:
        GridLayout:
            id: tracklist
            cols: 1
            size_hint_y: None
            height: self.minimum_height
            spacing: dp(2)

    Label:
        id: now
        text: 'Ничего не играет'
        size_hint_y: None
        height: dp(40)
        text_size: self.size
        halign: 'center'
        valign: 'middle'
        shorten: True

    Slider:
        id: seek
        size_hint_y: None
        height: dp(36)
        min: 0
        max: 1

    BoxLayout:
        size_hint_y: None
        height: dp(56)
        spacing: dp(6)
        Button:
            text: 'Назад'
            on_release: app.prev()
        Button:
            id: pp
            text: 'Играть'
            on_release: app.toggle()
        Button:
            text: 'Вперёд'
            on_release: app.next()
"""


class MusicApp(App):
    title = "TG Music"

    # ---------- запуск ----------
    def build(self):
        self.client = None
        self.cfg = {}
        self.tracks = []
        self.index = -1
        self.sound = None
        self.paused = False
        self.pause_pos = 0.0
        self.dragging = False
        self.play_task = None

        self.cfg_path = os.path.join(self.user_data_dir, "config.json")
        self.session_path = os.path.join(self.user_data_dir, "session")
        self.cache_dir = os.path.join(self.user_data_dir, "cache")
        os.makedirs(self.cache_dir, exist_ok=True)

        root = Builder.load_string(KV)
        seek = root.ids.seek
        seek.bind(on_touch_down=self._seek_down, on_touch_up=self._seek_up)
        return root

    def on_start(self):
        Clock.schedule_interval(self.tick, 0.5)
        asyncio.ensure_future(self.startup())

    def on_pause(self):
        return True  # не убивать приложение при сворачивании

    def on_resume(self):
        pass

    # ---------- утилиты UI ----------
    def set_status(self, text):
        self.root.ids.status.text = text

    def ask(self, title, hint="", password=False):
        """Показывает окно с полем ввода и возвращает Future со строкой."""
        fut = asyncio.get_running_loop().create_future()
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(8))
        ti = TextInput(
            multiline=False,
            password=password,
            hint_text=hint,
            size_hint_y=None,
            height=dp(44),
        )
        btn = Button(text="OK", size_hint_y=None, height=dp(44))
        box.add_widget(ti)
        box.add_widget(btn)
        pop = Popup(title=title, content=box, size_hint=(0.9, 0.4), auto_dismiss=False)

        def done(*_):
            if not fut.done():
                fut.set_result(ti.text.strip())
            pop.dismiss()

        btn.bind(on_release=done)
        ti.bind(on_text_validate=done)
        pop.open()
        ti.focus = True
        return fut

    # ---------- Telegram ----------
    async def startup(self):
        try:
            if os.path.exists(self.cfg_path):
                with open(self.cfg_path, encoding="utf-8") as f:
                    self.cfg = json.load(f)

            prompts = [
                ("api_id", "API ID", "число с my.telegram.org"),
                ("api_hash", "API hash", "строка с my.telegram.org"),
                ("channel", "Канал", "@username, ссылка t.me/... или числовой id"),
            ]
            for key, title, hint in prompts:
                if not self.cfg.get(key):
                    self.cfg[key] = await self.ask(title, hint)
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f)

            self.set_status("Подключение...")
            self.client = TelegramClient(
                self.session_path, int(self.cfg["api_id"]), self.cfg["api_hash"]
            )
            await self.client.connect()

            if not await self.client.is_user_authorized():
                phone = await self.ask("Телефон", "+79991234567")
                await self.client.send_code_request(phone)
                code = await self.ask("Код из Telegram", "12345")
                try:
                    await self.client.sign_in(phone, code)
                except SessionPasswordNeededError:
                    pwd = await self.ask("Пароль 2FA", password=True)
                    await self.client.sign_in(password=pwd)

            await self.load_tracks()
        except Exception as e:  # noqa: BLE001
            self.set_status(f"Ошибка: {e} (кнопка «Сброс»)")

    async def load_tracks(self):
        try:
            self.set_status("Загружаю список...")
            ch = self.cfg["channel"].strip()
            if ch.lstrip("-").isdigit():
                ch = int(ch)
                await self.client.get_dialogs()  # чтобы id канала был известен
            entity = await self.client.get_entity(ch)

            tracks = []
            async for m in self.client.iter_messages(
                entity, limit=1000, filter=InputMessagesFilterMusic
            ):
                if not m.document:
                    continue
                title = performer = None
                duration = 0
                for a in m.document.attributes:
                    if isinstance(a, DocumentAttributeAudio):
                        title, performer = a.title, a.performer
                        duration = a.duration or 0
                name = m.file.name if m.file else None
                if not title:
                    title = os.path.splitext(name)[0] if name else f"Трек {m.id}"
                if performer:
                    title = f"{performer} — {title}"
                ext = (m.file.ext if m.file else None) or ".mp3"
                tracks.append(
                    {
                        "msg": m,
                        "title": title,
                        "duration": duration,
                        "path": os.path.join(self.cache_dir, f"{m.id}{ext}"),
                    }
                )
            self.tracks = tracks
            self.fill_list()
            self.set_status(f"Треков: {len(tracks)}")
        except Exception as e:  # noqa: BLE001
            self.set_status(f"Ошибка: {e}")

    def fill_list(self):
        grid = self.root.ids.tracklist
        grid.clear_widgets()
        for i, t in enumerate(self.tracks):
            b = Button(
                text=t["title"],
                size_hint_y=None,
                height=dp(52),
                halign="left",
                valign="middle",
                shorten=True,
                shorten_from="right",
            )
            b.bind(size=lambda w, s: setattr(w, "text_size", (s[0] - dp(16), s[1])))
            b.bind(on_release=lambda w, i=i: self.start(i))
            grid.add_widget(b)

    # ---------- воспроизведение ----------
    def start(self, i):
        if self.play_task and not self.play_task.done():
            self.play_task.cancel()
        self.play_task = asyncio.ensure_future(self.play(i))

    async def play(self, i):
        self.stop_sound()
        self.index = i
        t = self.tracks[i]
        self.root.ids.now.text = t["title"]
        self.root.ids.pp.text = "Играть"
        path = t["path"]
        try:
            if not os.path.exists(path):
                tmp = path + ".part"

                def progress(done, total):
                    if total:
                        self.set_status(f"Загрузка {done * 100 // total}%")

                await self.client.download_media(
                    t["msg"], file=tmp, progress_callback=progress
                )
                os.replace(tmp, path)
                self.set_status(f"Треков: {len(self.tracks)}")

            snd = SoundLoader.load(path)
            if not snd:
                self.set_status("Не удалось открыть файл")
                return
            snd.play()
            self.sound = snd
            self.paused = False
            seek = self.root.ids.seek
            seek.max = snd.length if snd.length and snd.length > 0 else max(t["duration"], 1)
            seek.value = 0
            self.root.ids.pp.text = "Пауза"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.set_status(f"Ошибка: {e}")

    def stop_sound(self):
        s, self.sound = self.sound, None
        if s:
            s.stop()

    def toggle(self):
        pp = self.root.ids.pp
        if not self.sound:
            if self.tracks:
                self.start(max(self.index, 0))
            return
        if self.paused:
            self.sound.play()
            self.sound.seek(self.pause_pos)
            self.paused = False
            pp.text = "Пауза"
        else:
            self.pause_pos = self.sound.get_pos()
            self.sound.stop()
            self.paused = True
            pp.text = "Играть"

    def next(self):
        if self.tracks:
            self.start((self.index + 1) % len(self.tracks))

    def prev(self):
        if self.tracks:
            self.start((self.index - 1) % len(self.tracks))

    def tick(self, _dt):
        s = self.sound
        if not s or self.paused:
            return
        if s.state == "play":
            seek = self.root.ids.seek
            if s.length and s.length > 0 and abs(seek.max - s.length) > 0.5:
                seek.max = s.length
            if not self.dragging:
                seek.value = min(s.get_pos(), seek.max)
        else:  # трек доиграл — следующий
            self.sound = None
            self.next()

    def _seek_down(self, w, touch):
        if w.collide_point(*touch.pos):
            self.dragging = True

    def _seek_up(self, w, touch):
        if self.dragging:
            self.dragging = False
            if self.sound:
                self.sound.seek(w.value)
            if self.paused:
                self.pause_pos = w.value

    # ---------- обновить / сбросить ----------
    def refresh(self):
        if self.client:
            asyncio.ensure_future(self.load_tracks())
        else:
            asyncio.ensure_future(self.startup())

    def reset(self):
        asyncio.ensure_future(self.do_reset())

    async def do_reset(self):
        self.stop_sound()
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self.client = None
        for p in (self.cfg_path, self.session_path + ".session"):
            if os.path.exists(p):
                os.remove(p)
        shutil.rmtree(self.cache_dir, ignore_errors=True)
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cfg = {}
        self.tracks = []
        self.index = -1
        self.fill_list()
        await self.startup()


if __name__ == "__main__":
    asyncio.run(MusicApp().async_run(async_lib="asyncio"))
