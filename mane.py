"""
СБИС ↔ Аптеки — матчинг счетов-фактур
Задание: https://app.simulative.ru/course/65/1076
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pandas as pd

# ─── Константы ────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent

INCOMING_DIR = BASE_DIR / "Входящие"
APTEKI_DIR = BASE_DIR / "Аптеки" / "csv" / "correct"
RESULT_ROOT = BASE_DIR / "Результат"

# Имена столбцов датафрейма СБИС (в том же порядке, что в csv)
SBIS_COLUMNS = [
    "Дата", "Номер", "Сумма", "Статус", "Примечание", "Комментарий",
    "Контрагент", "ИНН/КПП", "Организация", "ИНН/КПП", "Тип документа",
    "Имя файла", "Дата", "Номер 1", "Сумма 1", "Сумма НДС", "Ответственный",
    "Подразделение", "Код", "Дата", "Время", "Тип пакета",
    "Идентификатор пакета", "Запущено в обработку", "Получено контрагентом",
    "Завершено", "Увеличение суммы", "НДC", "Уменьшение суммы", "НДС",
]

# Типы документов, которые берём в расчёт
ALLOWED_DOC_TYPES = {"СчФктр", "УпдДоп", "УпдСчфДоп", "ЭДОНакл"}

# Итоговый порядок столбцов в выходном файле
FINAL_COLUMNS = [
    "№ п/п", "Штрих-код партии", "Наименование товара", "Поставщик",
    "Дата приходного документа", "Номер приходного документа",
    "Дата накладной", "Номер накладной", "Номер счет-фактуры",
    "Сумма счет-фактуры", "Кол-во",
    "Сумма в закупочных ценах без НДС", "Ставка НДС поставщика",
    "Сумма НДС", "Сумма в закупочных ценах с НДС",
    "Дата счет-фактуры", "Сравнение дат",
]


# ─── Вспомогательные функции ───────────────────────────────────────────────────

def _to_snake(name: str) -> str:
    """'ИНН/КПП' → 'ИНН_КПП',  'Тип пакета' → 'Тип_пакета'."""
    return re.sub(r"[\s/\-]+", "_", name.strip())


def _parse_date(value) -> str:
    """Приводит дату к виду ДД.ММ.ГГГГ; при неудаче возвращает пустую строку."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, (pd.Timestamp,)):
        return value.strftime("%d.%m.%Y")
    s = str(value).strip()
    if not s or s.lower() in ("nat", "nan", "none"):
        return ""
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y", "%d/%m/%Y"):
        try:
            from datetime import datetime
            return datetime.strptime(s, fmt).strftime("%d.%m.%Y")
        except ValueError:
            continue
    return s  # отдаём «как есть», если формат неизвестен


# ─── Загрузка СБИС ────────────────────────────────────────────────────────────

def load_sbis(incoming_dir: Path) -> pd.DataFrame:
    """Читаем все csv из папки Входящие, объединяем в один датафрейм."""
    frames: list[pd.DataFrame] = []

    for path in sorted(incoming_dir.iterdir()):
        if path.suffix.lower() != ".csv":
            print(f"  [skip] {path.name}  — не csv")
            continue
        try:
            df = pd.read_csv(
                path,
                sep=";",
                encoding="utf-8",
                header=0,
                dtype=str,        # всё читаем как строки, потом разберёмся
            )
        except UnicodeDecodeError:
            df = pd.read_csv(path, sep=";", encoding="cp1251", header=0, dtype=str)

        # Назначаем имена по позиции (задание требует ровно SBIS_COLUMNS)
        if df.shape[1] == len(SBIS_COLUMNS):
            df.columns = SBIS_COLUMNS
        else:
            print(
                f"  [warn] {path.name}: ожидали {len(SBIS_COLUMNS)} столбцов, "
                f"нашли {df.shape[1]} — пропускаем переименование"
            )

        frames.append(df)
        print(f"  [ok]   {path.name}  ({len(df)} строк)")

    if not frames:
        raise FileNotFoundError(f"Нет csv-файлов в папке {incoming_dir}")

    combined = pd.concat(frames, ignore_index=True)

    # snake_case имён
    combined.columns = [_to_snake(c) for c in combined.columns]

    # Убираем пробелы в строковых полях
    for col in combined.columns:
        combined[col] = combined[col].astype(str).str.strip()

    print(f"\nСБИС загружен: {len(combined)} строк, {combined.shape[1]} столбцов\n")
    return combined


# ─── Обработка одной аптеки ───────────────────────────────────────────────────

def process_apteka(df_apt: pd.DataFrame, df_sbis: pd.DataFrame) -> pd.DataFrame:
    """
    Добавляет к датафрейму аптеки столбцы:
        Номер счет-фактуры, Сумма счет-фактуры, Дата счет-фактуры, Сравнение дат
    Возвращает датафрейм с FINAL_COLUMNS.
    """
    # Добавляем новые столбцы
    df_apt = df_apt.copy()
    df_apt["Номер счет-фактуры"] = ""
    df_apt["Сумма счет-фактуры"] = ""
    df_apt["Дата счет-фактуры"] = ""
    df_apt["Сравнение дат"] = ""

    # snake_case имён столбцов СБИС для удобного обращения
    # После load_sbis у нас уже snake_case, поэтому просто определяем нужные
    col_num  = _to_snake("Номер")           # "Номер"
    col_sum  = _to_snake("Сумма")           # "Сумма"
    col_date = _to_snake("Дата")            # "Дата"  — первая колонка
    col_type = _to_snake("Тип документа")   # "Тип_документа"

    # Фильтруем СБИС по допустимым типам документов
    sbis_ok = df_sbis[df_sbis[col_type].isin(ALLOWED_DOC_TYPES)].copy()

    # Строим индекс: номер → строки (для быстрого поиска)
    sbis_ok_index = sbis_ok.set_index(col_num)

    for idx, row in df_apt.iterrows():
        supplier    = str(row.get("Поставщик", "")).strip()
        inv_num_raw = str(row.get("Номер накладной", "")).strip()
        inv_date    = _parse_date(row.get("Дата накладной", ""))

        if not inv_num_raw or inv_num_raw == "nan":
            continue

        # Правило 1: ЕАПТЕКА → добавляем /15
        inv_num = inv_num_raw + "/15" if supplier.upper() == "ЕАПТЕКА" else inv_num_raw

        # Поиск в СБИС
        if inv_num not in sbis_ok_index.index:
            continue

        match = sbis_ok_index.loc[inv_num]

        # Если совпадений несколько — берём первую строку (Series vs DataFrame)
        if isinstance(match, pd.DataFrame):
            match = match.iloc[0]

        found_num  = str(match[col_num]).strip()
        found_sum  = str(match[col_sum]).strip()
        found_date = _parse_date(match[col_date])

        df_apt.at[idx, "Номер счет-фактуры"] = found_num
        df_apt.at[idx, "Сумма счет-фактуры"] = found_sum
        df_apt.at[idx, "Дата счет-фактуры"]  = found_date

        # Сравнение дат: пустая дата с/ф при заполненной дате накладной → расхождение
        if not found_date and inv_date:
            df_apt.at[idx, "Сравнение дат"] = "Не совпадает!"
        elif found_date and inv_date and found_date != inv_date:
            df_apt.at[idx, "Сравнение дат"] = "Не совпадает!"

    # Приводим к итоговым столбцам (недостающие создаём пустыми)
    for col in FINAL_COLUMNS:
        if col not in df_apt.columns:
            df_apt[col] = ""

    return df_apt[FINAL_COLUMNS].copy()


# ─── Главная функция ──────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("СБИС ↔ Аптеки — матчинг счетов-фактур")
    print("=" * 60)

    # 1. Загружаем СБИС
    print(f"\n[1/3] Загружаем СБИС из {INCOMING_DIR} …")
    df_sbis = load_sbis(INCOMING_DIR)

    # 2. Готовим папку для результатов (дата сегодня)
    today_str  = date.today().strftime("%d.%m.%Y")   # «30.06.2026»
    result_dir = RESULT_ROOT / today_str
    result_dir.mkdir(parents=True, exist_ok=True)
    print(f"[2/3] Результаты будут сохранены в: {result_dir}\n")

    # 3. Обрабатываем каждый файл аптеки
    print(f"[3/3] Обрабатываем аптеки из {APTEKI_DIR} …\n")

    processed = 0
    for path in sorted(APTEKI_DIR.iterdir()):
        if path.suffix.lower() != ".csv":
            print(f"  [skip] {path.name}  — не csv")
            continue

        print(f"  → {path.name}")
        try:
            df_apt = pd.read_csv(path, sep=";", encoding="utf-8", header=0, dtype=str)
        except UnicodeDecodeError:
            df_apt = pd.read_csv(path, sep=";", encoding="cp1251", header=0, dtype=str)

        # Убираем пробелы в заголовках и значениях
        df_apt.columns = [c.strip() for c in df_apt.columns]
        for col in df_apt.columns:
            df_apt[col] = df_apt[col].astype(str).str.strip()

        df_result = process_apteka(df_apt, df_sbis)

        out_path = result_dir / f"{path.stem} - результат.xlsx"
        df_result.to_excel(out_path, index=False)
        print(f"     сохранено: {out_path}  ({len(df_result)} строк)")
        processed += 1

    print(f"\nГотово! Обработано файлов аптек: {processed}")
    print(f"Результаты: {result_dir}")


if __name__ == "__main__":
    main()
