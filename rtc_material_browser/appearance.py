"""Per-profile UI language and palette, independent of the renderer."""
from dataclasses import dataclass


BG = (0.046, 0.055, 0.061, 1)
PANEL = (0.066, 0.078, 0.087, 1)
CARD = (0.081, 0.095, 0.104, 1)
BUTTON = (0.101, 0.118, 0.127, 1)
HOVER = (0.143, 0.168, 0.178, 1)
LINE = (0.151, 0.174, 0.184, 1)
TEXT = (0.93, 0.96, 0.95, 1)
MUTED = (0.65, 0.72, 0.74, 1)
MATTE = (0.024, 0.028, 0.031, 1)
DEFAULT_ACCENT = (205 / 255, 53 / 255, 45 / 255)


def mix_color(start, end, amount):
    return tuple(a + (b - a) * amount for a, b in zip(start, end))


@dataclass(frozen=True)
class Palette:
    accent: tuple
    dark: tuple
    label: tuple
    border: tuple
    hover: tuple
    ink: tuple


def palette(rgb):
    accent = (*rgb, 1)
    hover = mix_color(accent, (1, 1, 1, 1), 0.18)
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
              for value in rgb]
    luminance = sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
    ink = (1, 1, 1, 1) if luminance < 0.179 else (0.025, 0.025, 0.025, 1)
    return Palette(accent, mix_color(PANEL, accent, 0.28), mix_color(accent, TEXT, 0.40),
                   mix_color(LINE, accent, 0.65), hover, ink)


theme = palette(DEFAULT_ACCENT)
language = "EN"


def configure(value, rgb):
    global language, theme
    language = value
    theme = palette(rgb)


RU = {
    "Texture library": "Библиотека текстур",
    "Interface": "Интерфейс",
    "Accent color": "Цвет акцента",
    "Language": "Язык",
    "Maintenance": "Обслуживание",
    "Index": "Индексация",
    "Read": "Прочитать",
    "Cache SVN": "Кэш авторов SVN",
    "Open a material in Material Core to use Read.": "Для чтения откройте материал в Material Core.",
    "Choose the texture library folder in Material Core preferences.": "Укажите папку библиотеки текстур в настройках Material Core.",
    "Choose Image Editor in the area where Material Core should open.": "Выберите Image Editor в области для Material Core.",
    "Material Core is already open in this workspace.": "Material Core уже открыт в этой рабочей области.",
    "Blender could not split this 3D View.": "Не удалось разделить область 3D View.",
    "Blender could not open a separate window.": "Не удалось открыть отдельное окно Blender.",
    "An operation is already running.": "Операция уже выполняется.",
    "Index the library first.": "Сначала проиндексируйте библиотеку.",
    "Choose an editable mesh in Object Mode.": "Выберите редактируемый меш в объектном режиме.",
    "Choose a mesh in Object Mode.": "Выберите меш в объектном режиме.",
    "All folders": "Все папки",
    "All tags": "Все теги",
    "All collections": "Все коллекции",
    "Favorites": "Избранное",
    "Ignore List": "Скрытые",
    "Buildings": "Здания",
    "Search names or tags": "Поиск по имени или тегам",
    "No matching materials": "Материалы не найдены",
    "Enlarge this area to display cards": "Увеличьте область, чтобы увидеть карточки",
    "4L palette": "Палитра 4L",
    "Back": "Назад",
    "Apply": "Применить",
    "Apply to selected": "На выбранное",
    "Edit tags": "Теги",
    "Add tag": "Добавить тег",
    "Add": "Добавить",
    "Cancel": "Отмена",
    "Create": "Создать",
    "Save": "Сохранить",
    "New collection": "Новая коллекция",
    "+ Collection": "+ Коллекция",
    "Collection": "Коллекция",
    "Add to collection": "В коллекцию",
    "Open on SVN": "Открыть на SVN",
    "Open first layer on SVN": "Открыть первый слой на SVN",
    "Add to 4L palette": "В палитру 4L",
    "Load layers into 4L palette": "Загрузить слои в палитру 4L",
    "To Ignore List": "Скрыть материал",
    "Remove from Ignore List": "Вернуть материал",
    "Open SVN folder": "Открыть папку на SVN",
    "Remove from Favorites": "Убрать из избранного",
    "Add to Favorites": "В избранное",
    "Drop on a slot to reorder · Drop in the catalog to remove": "Слот меняет порядок · Сброс в каталог удаляет слой",
    "Drop on a palette slot or a mesh": "Перетащите в слот палитры или на меш",
    "Drop on a mesh in another 3D View": "Перетащите на меш в соседней области 3D View",
    "Enlarge this area to edit tags": "Увеличьте область для редактирования тегов",
    "Tags": "Теги",
    "Type a tag or paste a comma-separated list": "Введите тег или список через запятую",
    "Enter to add": "Enter — добавить",
    "Layer order": "Порядок слоёв",
    "Automatic": "Автоматически",
    "Local 4L preset": "Локальный пресет 4L",
    "Shared 4L preset": "Общий пресет 4L",
    "Shared Catalog": "Общий каталог",
    "Enter a name": "Введите название",
    "No tags yet": "Тегов пока нет",
    "Tags saved": "Теги сохранены",
    "Material metadata saved": "Данные материала сохранены",
    "Use Load layers into 4L palette to replace the current mix.": "Чтобы заменить смесь, загрузите слои в палитру 4L.",
    "All four slots are filled. Remove a layer first.": "Все четыре слота заняты. Сначала удалите слой.",
    "Missing layer": "Слой отсутствует",
    "Cover": "Обложка",
    "Interactive 3D": "Интерактивное 3D",
    "Loading texture…": "Загрузка текстуры…",
    "No cover available": "Обложка недоступна",
    "Texture": "Текстура",
    "Diffuse": "Цвет",
    "Normal": "Нормали",
    "Roughness": "Шероховатость",
    "Metalic": "Металличность",
    "Alpha": "Альфа",
    "Emission": "Свечение",
    "aoMap": "Затенение",
    "Standart": "Стандарт",
    "Decal": "Декаль",
    "Plane": "Плоскость",
    "Sphere": "Сфера",
    "Reset": "Сброс",
    "Loading texture maps…": "Загрузка карт…",
    "Drag to orbit  ·  Scroll to zoom": "Мышь — поворот · Колесо — масштаб",
    "Moved to Ignore List": "Материал скрыт",
    "Removed from Ignore List": "Материал возвращён",
    "Layer removed": "Слой удалён",
    "First SVN upload · author unavailable": "Первое добавление в SVN · автор недоступен",
    "First SVN upload · use Read in settings": "Первое добавление в SVN · запрос в настройках",
    "First SVN upload": "Первое добавление в SVN",
    "Unknown": "Неизвестно",
    "Indexing textures…": "Индексация текстур…",
    "Reading SVN history…": "Чтение истории SVN…",
    "Caching SVN authors…": "Кэширование авторов SVN…",
    "Loading gallery thumbnails…": "Загрузка миниатюр…",
    "Loading texture preview…": "Загрузка карты…",
    "Preparing 4L preview…": "Подготовка превью 4L…",
    "Preparing large cover…": "Подготовка обложки…",
    "Click Index to scan this catalog.": "Запустите индексацию в настройках Material Core.",
    "SVN host not found.": "Адрес сервера SVN не найден.",
    "SVN server unreachable.": "Сервер SVN недоступен.",
    "SVN access denied.": "SVN отклонил доступ.",
    "Enable Online Access in Blender to download the SVN client.": "Для загрузки клиента SVN разрешите доступ к сети в Blender.",
    "The downloaded SVN client failed integrity verification.": "Не удалось проверить целостность клиента SVN.",
    "The SVN client archive is incomplete.": "Архив клиента SVN неполный.",
    "SVN history has no original Add for this texture.": "В истории SVN нет записи о первом добавлении текстуры.",
    "Material has no source texture.": "У материала отсутствует исходная текстура.",
    "Enter a collection name.": "Введите название коллекции.",
    "A collection already uses that name.": "Коллекция с таким именем уже есть.",
    "Enter a name for the 4L material.": "Введите имя материала 4L.",
    "A saved 4L material already uses that name.": "Материал 4L с таким именем уже есть.",
    "Choose one to four distinct layers from this material.": "Выберите от одного до четырёх разных слоёв этого материала.",
    "Shared metadata changed. Reopen the editor before saving.": "Общие данные изменились. Откройте редактор заново.",
    "Preset tags changed. Reopen the editor before saving.": "Теги пресета изменились. Откройте редактор заново.",
    "This folder is missing from the SVN working copy.": "Этой папки нет в рабочей копии SVN.",
    "Blender did not return the OpenEXR preview.": "Blender не создал превью OpenEXR.",
    "Background operation failed.": "Фоновая операция завершилась ошибкой.",
    "{count} materials": "Материалы: {count}",
    "Catalog previews  {progress}": "Обложки  {progress}",
    "{count} new previews": "Новых обложек: {count}",
    "{count} unreadable": "Не удалось прочитать: {count}",
    "SVN authors: {found}/{total}": "Авторы SVN: {found}/{total}",
    "Created collection: {name}": "Коллекция создана: {name}",
    "Add 4L preset to {title}": "Добавить пресет 4L в «{title}»",
    "Added to {title}: {name}": "Добавлено в «{title}»: {name}",
    "Remove from {title}": "Убрать из «{title}»",
    "Removed from {title}": "Убрано из «{title}»",
    "Added to {title}": "Добавлено в «{title}»",
    "Loaded {name}": "Загружено: {name}",
    "Added {name}": "Добавлено: {name}",
    "Applied to {name}": "Применено к «{name}»",
    "Layer {index}/{count}": "Слой {index}/{count}",
    "1–4 keys, comma-separated · {keys}": "1–4 ключа через запятую · {keys}",
}


def tr(message, **values):
    label = RU.get(message, message) if language == "RU" else message
    return label.format(**values) if values else label
