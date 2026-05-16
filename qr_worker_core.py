import csv
import json
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

INPUT_CSV = "phone_numbers.csv"
RETRY_CSV = "retry_numbers.csv"
ALIASES_FILE = "email_aliases.json"

PROFILES_DIR = "chrome_profiles"

WAIT_TIMEOUT = 30
HEADLESS = False
CLICK_PAUSE = 0.03
INPUT_PAUSE = 0.02
BETWEEN_NUMBERS_PAUSE = 0.08

ROOT_URL = "https://lk-b2b.mts.ru/"
WEB_URL = "https://lk-b2b.mts.ru/mts_business_web/"
AUTH_URL = "https://lk-b2b.mts.ru/mts_business_auth/?return_url=https%3A%2F%2Flk-b2b.mts.ru%2Fmts_business_web"

DEFAULT_EMAIL_ALIASES = {
    "memrik": "memrik007@gmail.com",
    "maliw": "Doduki462@yandex.ru",
    "hitroy": "maksogysok2019@gmail.com",
    "cunt": "svoyts@list.ru",
    "sebe": "sobidjonpy@gmail.com",
    "kamen": "antonbulba6767@gmail.com",
}

XPATH_BTN_OPEN = "/html/body/vm-app/vm-main/vm-app-wrapper/mts-root/div/div/div[3]/div/main/div/div[2]/vm-terminal-device/c-wrapper/c-wrapper-section[2]/div/div[2]/vm-general/c-wrapper/c-wrapper-section/div/div[1]/div[2]/vm-user-data-general/c-card/mts-card/c-card-body/mts-card-body/div[2]/c-button[1]/mts-button"
XPATH_BTN_CHANGE_SIM = "//*[starts-with(@id,'mat-dialog-')]/vm-modal/div[2]/c-renderer/vm-operation-modal-container/vm-operation-container/vm-change-sim/vm-operation-wrapper/vm-operation-content/div/div[1]/vm-change-sim-buttons[2]/div/div[1]"
XPATH_ALREADY_CHANGED_BTN = "//*[starts-with(@id,'mat-dialog-')]/vm-modal/div[2]/c-renderer/vm-operation-modal-container/vm-operation-container/vm-change-sim/vm-operation-wrapper/vm-operation-content/div/div[1]/vm-change-sim-buttons[3]"
XPATH_EMAIL_INPUT = "//*[@id='c-input-id-5'] | //*[starts-with(@id,'mat-dialog-')]//input[contains(@id,'c-input-id-')]"
XPATH_BTN_CONFIRM = "//*[starts-with(@id,'mat-dialog-')]/vm-modal/div[2]/c-renderer/vm-operation-modal-container/vm-operation-container/vm-change-sim/vm-operation-wrapper/vm-operation-content/vm-operation-footer/div/div/div[2]/c-button[2]/mts-button"
XPATH_SUCCESS_MODAL = "//mat-dialog-container[starts-with(@id,'mat-dialog-')]//vm-operation-success"
XPATH_SUCCESS_TITLE = "//mat-dialog-container[starts-with(@id,'mat-dialog-')]//*[contains(normalize-space(.), 'Заявка принята')]"


class SkipNumber(Exception):
    pass


class StepError(Exception):
    def __init__(self, step, message):
        self.step = step
        self.message = message
        super().__init__(f"{step}: {message}")


def _safe_alias(alias: str) -> str:
    return "".join(ch for ch in alias.lower() if ch.isalnum() or ch in ("-", "_"))


def profile_path(alias: str) -> Path:
    Path(PROFILES_DIR).mkdir(parents=True, exist_ok=True)
    return Path(PROFILES_DIR) / f"profile_{_safe_alias(alias)}"


def setup_driver(headless=False, alias: str = None):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")
    else:
        options.add_argument("--start-maximized")

    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-notifications")
    options.add_argument("--log-level=3")

    if alias:
        p = profile_path(alias)
        options.add_argument(f"--user-data-dir={str(p.resolve())}")
        options.add_argument("--profile-directory=Default")

    prefs = {"profile.managed_default_content_settings.images": 2}
    options.add_experimental_option("prefs", prefs)
    options.page_load_strategy = "eager"

    return webdriver.Chrome(options=options)


def _looks_like_logged_in(driver) -> bool:
    try:
        cur = (driver.current_url or "").lower()
        if "lk-b2b.mts.ru" not in cur:
            return False
        if "mts_business_auth" in cur or "auth" in cur or "login" in cur:
            return False
        if "mts_business_web" not in cur:
            return False

        markers = ["//vm-app", "//vm-main", "//mts-root"]
        return any(driver.find_elements(By.XPATH, xp) for xp in markers)
    except Exception:
        return False


def manual_login(driver, timeout=300):
    driver.get(AUTH_URL)
    print(f"\n=== [{AUTH_URL}] РУЧНАЯ АВТОРИЗАЦИЯ ===")
    print("Войди в аккаунт в открытом окне браузера...")
    print(f"Ожидание до {timeout} сек...")

    end = time.time() + timeout
    stable = 0

    while time.time() < end:
        if _looks_like_logged_in(driver):
            stable += 1
            if stable >= 3:
                print("✅ Авторизация подтверждена.\n")
                return
        else:
            stable = 0
        time.sleep(1)

    raise Exception("Авторизация не завершена (таймаут).")


def save_session(driver, alias: str):
    print(f"ℹ️ [{alias}] profile mode: save_session skipped (данные уже в Chrome profile)")


def load_session(driver, alias: str) -> bool:
    driver.get(WEB_URL)
    time.sleep(2)
    ok = _looks_like_logged_in(driver)
    print(f"ℹ️ [{alias}] profile mode: load_session -> {ok}")
    return ok


def load_aliases():
    path = Path(ALIASES_FILE)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                return {str(k).strip().lower(): str(v).strip() for k, v in data.items()}
        except Exception:
            pass
    path.write_text(json.dumps(DEFAULT_EMAIL_ALIASES, ensure_ascii=False, indent=2), encoding="utf-8")
    return dict(DEFAULT_EMAIL_ALIASES)


def normalize_phone(s):
    digits = "".join(ch for ch in str(s) if ch.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits if len(digits) >= 10 else None


def read_phones(csv_path):
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {csv_path}")

    phones = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        lines = [line.strip() for line in f if line.strip()]

    if not lines:
        return []

    if all((";" not in ln and "," not in ln and "\t" not in ln) for ln in lines):
        for ln in lines:
            p = normalize_phone(ln)
            if p:
                phones.append(p)
        return list(dict.fromkeys(phones))

    first = lines[0]
    delimiter = ";" if ";" in first else ("," if "," in first else "\t")
    rows = list(csv.reader(lines, delimiter=delimiter))

    header = [c.strip().lower() for c in rows[0]] if rows else []
    idx = header.index("phone") if "phone" in header else 0
    data_rows = rows[1:] if "phone" in header else rows

    for row in data_rows:
        if row and idx < len(row):
            p = normalize_phone(row[idx])
            if p:
                phones.append(p)

    return list(dict.fromkeys(phones))


def append_phone_if_not_exists(csv_path: str, phone: str):
    p = Path(csv_path)
    existing = set()
    if p.exists():
        with open(p, "r", encoding="utf-8-sig", newline="") as f:
            for line in f:
                v = normalize_phone(line.strip())
                if v:
                    existing.add(v)

    n = normalize_phone(phone)
    if not n or n in existing:
        return

    with open(p, "a", encoding="utf-8-sig", newline="") as f:
        f.write(n + "\n")


def remove_phone_from_file(csv_path: str, phone: str):
    p = Path(csv_path)
    if not p.exists():
        return

    target = normalize_phone(phone)
    if not target:
        return

    rows = []
    with open(p, "r", encoding="utf-8-sig", newline="") as f:
        for line in f:
            v = normalize_phone(line.strip())
            if v and v != target:
                rows.append(v)

    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        for v in rows:
            f.write(v + "\n")


def wait_click(driver, xpath, timeout=WAIT_TIMEOUT):
    try:
        el = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((By.XPATH, xpath)))
    except TimeoutException:
        raise StepError("wait_click", f"Элемент не кликабелен: {xpath[:70]}...")
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(CLICK_PAUSE)
    try:
        el.click()
    except Exception:
        driver.execute_script("arguments[0].click();", el)


def wait_input(driver, xpath, text, timeout=WAIT_TIMEOUT):
    try:
        el = WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.XPATH, xpath)))
    except TimeoutException:
        raise StepError("wait_input", "Поле email не найдено")
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(INPUT_PAUSE)
    el.clear()
    el.send_keys(text)


def wait_modal_state(driver, timeout=WAIT_TIMEOUT):
    end = time.time() + timeout
    while time.time() < end:
        if driver.find_elements(By.XPATH, XPATH_ALREADY_CHANGED_BTN):
            return "already_changed"
        if driver.find_elements(By.XPATH, XPATH_BTN_CHANGE_SIM):
            return "ready"
        time.sleep(0.05)
    raise StepError("wait_modal_state", "Состояние модалки не определено")


def wait_success_modal_required(driver, timeout=WAIT_TIMEOUT):
    try:
        WebDriverWait(driver, timeout).until(EC.visibility_of_element_located((By.XPATH, XPATH_SUCCESS_MODAL)))
        WebDriverWait(driver, timeout).until(EC.visibility_of_element_located((By.XPATH, XPATH_SUCCESS_TITLE)))
    except TimeoutException:
        raise StepError("wait_success_modal", "Не появилось подтверждение 'Заявка принята'")


def process_one(driver, phone, email):
    url = (
        "https://lk-b2b.mts.ru/mts_business_web/card/td/general"
        f"?td={phone}&from=numbers&previous=%2Fbilling%2Fflow%2Fselect%3Fview%3D0"
    )
    try:
        driver.get(url)
    except Exception as e:
        raise StepError("open_card", f"Не открыть карточку: {str(e).splitlines()[0][:120]}")

    wait_click(driver, XPATH_BTN_OPEN)
    state = wait_modal_state(driver)

    if state == "already_changed":
        raise SkipNumber("Номер уже обработан ранее")

    wait_click(driver, XPATH_BTN_CHANGE_SIM)
    wait_input(driver, XPATH_EMAIL_INPUT, email)
    wait_click(driver, XPATH_BTN_CONFIRM)
    wait_success_modal_required(driver)