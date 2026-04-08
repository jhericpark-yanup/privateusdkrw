# ============================================================
# USD/KRW Quantitative Swing Trading System v5.0 (Railway)
# Colab → Railway 변환:
#   - google.colab.auth 제거 → 서비스 계정 JSON 방식
#   - 민감 정보 환경변수(os.environ)로 분리
# ============================================================

import warnings
warnings.filterwarnings("ignore")

import os
import io
import json
import time
import datetime
import schedule
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import requests
import gspread
import yfinance as yf

from sklearn.linear_model import LinearRegression
from sklearn.preprocessing import StandardScaler
from google.oauth2.service_account import Credentials

# ─────────────────────────────────────────────
# 1. CONFIG  (민감 정보는 모두 환경변수에서 읽음)
# ─────────────────────────────────────────────
CONFIG = {
    # ── Telegram (Railway 환경변수에서 읽음) ──
    "telegram_token"   : os.environ.get("TELEGRAM_TOKEN", ""),
    "telegram_chat_id" : os.environ.get("TELEGRAM_CHAT_ID", ""),

    # ── Google Sheets (환경변수에서 읽음) ─────
    "sheet_name"       : os.environ.get("SHEET_NAME", "USDKRW_Quant_v5"),
    # GOOGLE_CREDENTIALS_JSON : 서비스 계정 JSON 전체를 문자열로 붙여넣기

    # ── Data ──────────────────────────────────
    "tickers": {
        "usdkrw": "KRW=X",
        "dxy"   : "DX-Y.NYB",
        "vix"   : "^VIX",
        "oil"   : "CL=F",
        "kospi" : "^KS11",
    },
    "period"  : "2y",
    "interval": "1d",

    # ── Signal ────────────────────────────────
    "z_exit"          : 0.3,
    "z_stoploss"      : 3.0,
    "z_warning_ratio" : 0.9,
    "ma_trend"        : 200,

    # ── Risk ──────────────────────────────────
    "default_capital"  : 10_000_000,

    # ── Schedule ──────────────────────────────
    "daily_report_time": "00:00",  # UTC 00:00 = KST 09:00
    "data_refresh_min" : 60,
    "poll_interval_sec": 2,
}

# ─────────────────────────────────────────────
# 2. GOOGLE SHEETS (서비스 계정 방식)
# ─────────────────────────────────────────────
POSITION_TAB = "POSITION"
LOG_TAB      = "LOG"

POS_COLS = ["active","direction","entry_price","entry_date","capital","updated_at"]
LOG_COLS = [
    "date","timestamp","spot","fair","z_score",
    "sys_signal","sys_action","regime",
    "pos_active","pos_direction","entry_price","entry_date",
    "current_price","holding_days","pnl_pct","pnl_krw","mfe_pct","mae_pct",
]

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

_gc = None

def get_gc():
    global _gc
    if _gc is not None:
        return _gc
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
    if not creds_json:
        raise RuntimeError("환경변수 GOOGLE_CREDENTIALS_JSON 가 설정되지 않았습니다.")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    _gc = gspread.authorize(creds)
    return _gc

def get_ws(tab: str, headers: list):
    gc = get_gc()
    try:
        sh = gc.open(CONFIG["sheet_name"])
    except gspread.SpreadsheetNotFound:
        sh = gc.create(CONFIG["sheet_name"])
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=2000, cols=len(headers))
        ws.append_row(headers)
    if ws.row_count == 0 or ws.cell(1,1).value != headers[0]:
        ws.insert_row(headers, 1)
    return ws

def read_position() -> dict:
    empty = {"active":False,"direction":"","entry_price":0.0,
             "entry_date":"","capital":CONFIG["default_capital"]}
    try:
        ws   = get_ws(POSITION_TAB, POS_COLS)
        rows = ws.get_all_records()
        if not rows:
            return empty
        last = rows[-1]
        if str(last.get("active","")).upper() != "TRUE":
            return empty
        return {
            "active"      : True,
            "direction"   : last["direction"],
            "entry_price" : float(last["entry_price"]),
            "entry_date"  : last["entry_date"],
            "capital"     : float(last.get("capital", CONFIG["default_capital"])),
        }
    except Exception as e:
        print(f"⚠️  포지션 읽기 실패: {e}")
        return empty

def write_position(active: bool, direction: str = "",
                   entry_price: float = 0.0, entry_date: str = "",
                   capital: float = None):
    try:
        ws = get_ws(POSITION_TAB, POS_COLS)
        ws.append_row([
            str(active), direction, entry_price, entry_date,
            capital or CONFIG["default_capital"],
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ])
    except Exception as e:
        print(f"⚠️  포지션 저장 실패: {e}")

def append_log(row: list):
    try:
        ws = get_ws(LOG_TAB, LOG_COLS)
        ws.append_row(row)
    except Exception as e:
        print(f"⚠️  로그 저장 실패: {e}")

# ─────────────────────────────────────────────
# 3. 시장 데이터 & 모델
# ─────────────────────────────────────────────
def load_data() -> pd.DataFrame:
    raw = {}
    for name, ticker in CONFIG["tickers"].items():
        df = yf.download(ticker, period=CONFIG["period"],
                         interval=CONFIG["interval"], progress=False)
        if df.empty:
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        raw[name] = df["Close"].rename(name)

    data = pd.concat(raw.values(), axis=1)
    data.index = pd.to_datetime(data.index)
    data.sort_index(inplace=True)
    data.dropna(subset=["usdkrw","dxy"], inplace=True)
    data.ffill(inplace=True)
    data.dropna(inplace=True)

    for col in data.columns:
        data[f"ret_{col}"] = np.log(data[col] / data[col].shift(1))
    data.dropna(inplace=True)
    return data

def fair_value_model(data: pd.DataFrame) -> pd.DataFrame:
    available = [c for c in ["dxy","vix","oil","kospi"] if c in data.columns]
    X_sc = StandardScaler().fit_transform(data[available].values)
    fair = LinearRegression().fit(X_sc, data["usdkrw"].values).predict(X_sc)

    data = data.copy()
    data["fair"]       = fair
    data["mispricing"] = data["usdkrw"] - data["fair"]

    rm = data["mispricing"].rolling(60).mean()
    rs = data["mispricing"].rolling(60).std()
    data["z_score"] = (data["mispricing"] - rm) / rs.replace(0, np.nan)
    data.dropna(subset=["z_score"], inplace=True)
    return data

def regime_filter(data: pd.DataFrame) -> pd.DataFrame:
    ma = data["usdkrw"].rolling(CONFIG["ma_trend"]).mean()
    data = data.copy()
    data["ma200"]  = ma
    data["regime"] = "NEUTRAL"
    data.loc[data["usdkrw"] > ma, "regime"] = "BULL"
    data.loc[data["usdkrw"] < ma, "regime"] = "BEAR"
    return data

def _raw_signal(z: pd.Series, entry: float, exit_: float) -> pd.Series:
    sig, pos = pd.Series(0.0, index=z.index), 0
    for i in range(1, len(z)):
        zp, zc = z.iloc[i-1], z.iloc[i]
        if pos == 0:
            if zp <= entry  and zc > entry:    pos = -1
            elif zp >= -entry and zc < -entry: pos =  1
        elif abs(zc) < exit_:
            pos = 0
        sig.iloc[i] = pos
    return sig

def optimise_threshold(data: pd.DataFrame) -> float:
    best_sh, best_t = -np.inf, 1.0
    for t in np.arange(0.5, 2.6, 0.1):
        sig = _raw_signal(data["z_score"], t, CONFIG["z_exit"])
        ret = sig.shift(1) * data["ret_usdkrw"]
        ret.dropna(inplace=True)
        if len(ret) == 0 or ret.std() == 0:
            continue
        sh = (ret.mean() / ret.std()) * np.sqrt(252)
        if sh > best_sh:
            best_sh, best_t = sh, t
    return round(best_t, 1)

def generate_system_signal(data: pd.DataFrame, z_entry: float) -> pd.DataFrame:
    signals, actions, pos = [], [], 0
    z, regime = data["z_score"].values, data["regime"].values

    for i in range(1, len(data)):
        zp, zc, reg, action = z[i-1], z[i], regime[i], "HOLD"

        if pos == -1 and zc > CONFIG["z_stoploss"]:
            pos = 0; action = "STOP_LOSS_EXIT"
        elif pos == 1 and zc < -CONFIG["z_stoploss"]:
            pos = 0; action = "STOP_LOSS_EXIT"
        elif pos != 0 and abs(zc) < CONFIG["z_exit"]:
            pos = 0; action = "EXIT"
        elif pos == 0:
            se = z_entry * (1.5 if reg == "BULL" else 1.0)
            le = z_entry * (1.5 if reg == "BEAR" else 1.0)
            if zp <= se and zc > se:     pos = -1; action = "ENTER_SHORT"
            elif zp >= -le and zc < -le: pos =  1; action = "ENTER_LONG"

        signals.append(pos)
        actions.append(action)

    data = data.copy()
    data["sys_signal"] = [0]      + signals
    data["sys_action"] = ["HOLD"] + actions
    return data

def refresh_data(data_ref: dict, z_ref: dict):
    try:
        d = load_data()
        d = fair_value_model(d)
        d = regime_filter(d)
        z = optimise_threshold(d)
        d = generate_system_signal(d, z)
        data_ref["data"]  = d
        z_ref["z_entry"]  = z
        print(f"[{_now()}] 🔄 데이터 갱신  Z_entry=±{z}")
    except Exception as e:
        print(f"데이터 갱신 오류: {e}")

# ─────────────────────────────────────────────
# 4. 수익률 계산
# ─────────────────────────────────────────────
def calc_pnl(data: pd.DataFrame, pos: dict) -> dict | None:
    if not pos["active"] or pos["entry_price"] <= 0:
        return None

    direction   = pos["direction"]
    entry_price = pos["entry_price"]
    entry_date  = pd.to_datetime(pos["entry_date"])
    capital     = pos["capital"]

    after = data[data.index >= entry_date].copy()
    if after.empty:
        return None

    current      = float(after["usdkrw"].iloc[-1])
    holding_days = (after.index[-1] - entry_date).days

    if direction == "SHORT":
        pnl_pct = (entry_price - current)               / entry_price * 100
        mfe_pct = (entry_price - after["usdkrw"].min()) / entry_price * 100
        mae_pct = (entry_price - after["usdkrw"].max()) / entry_price * 100
    else:
        pnl_pct = (current - entry_price)               / entry_price * 100
        mfe_pct = (after["usdkrw"].max() - entry_price) / entry_price * 100
        mae_pct = (after["usdkrw"].min() - entry_price) / entry_price * 100

    return {
        "direction"    : direction,
        "entry_price"  : entry_price,
        "entry_date"   : entry_date,
        "current_price": current,
        "holding_days" : holding_days,
        "capital"      : capital,
        "pnl_pct"      : round(pnl_pct, 3),
        "pnl_krw"      : round(capital * pnl_pct / 100, 0),
        "mfe_pct"      : round(mfe_pct, 3),
        "mae_pct"      : round(mae_pct, 3),
        "mfe_krw"      : round(capital * mfe_pct / 100, 0),
        "mae_krw"      : round(capital * mae_pct / 100, 0),
    }

# ─────────────────────────────────────────────
# 5. 텔레그램 송수신
# ─────────────────────────────────────────────
_last_update_id = 0

def send_telegram(text: str) -> None:
    token   = CONFIG["telegram_token"]
    chat_id = CONFIG["telegram_chat_id"]
    if not token or not chat_id:
        print(f"[Telegram 미설정]\n{text}\n")
        return

    # 텔레그램 단일 메시지 4096자 제한 → 초과 시 분할 발송
    MAX_LEN = 4000
    chunks  = [text[i:i+MAX_LEN] for i in range(0, len(text), MAX_LEN)]

    for chunk in chunks:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"},
                timeout=10,
            )
            if r.status_code != 200:
                print(f"Telegram 전송 오류 {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"Telegram 전송 예외: {e}")

def get_updates() -> list:
    global _last_update_id
    token = CONFIG["telegram_token"]
    if not token:
        return []
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": _last_update_id + 1, "timeout": 5},
            timeout=10,
        )
        updates = r.json().get("result", [])
        if updates:
            _last_update_id = updates[-1]["update_id"]
        return updates
    except Exception:
        return []

# ─────────────────────────────────────────────
# 6. 메시지 포맷
# ─────────────────────────────────────────────
def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

def fmt_entry_guide(data: pd.DataFrame, z_entry: float) -> str:
    latest  = data.iloc[-1]
    z       = float(latest["z_score"])
    regime  = latest["regime"]
    spot    = float(latest["usdkrw"])
    fair    = float(latest["fair"])

    short_thresh = round(z_entry * (1.5 if regime == "BULL" else 1.0), 2)
    long_thresh  = round(z_entry * (1.5 if regime == "BEAR" else 1.0), 2)
    short_gap    = round(short_thresh - z, 2)
    long_gap     = round(z - (-long_thresh), 2)

    # ── 1. 신호 강도 & 액션 단계 ─────────────────
    if z > 0:
        if z >= CONFIG["z_stoploss"]:
            strength, strength_desc, action_step = "🚨 위험 (스톱로스 구간)", "추세 이탈 위험 — 진입 자제", "관망"
        elif z >= short_thresh * 1.3:
            strength, strength_desc, action_step = "🔴🔴 매우 강한 SHORT", f"Z {z:.2f} — 강하게 고평가, 강진입 고려", "강진입"
        elif z >= short_thresh:
            strength, strength_desc, action_step = "🔴 강한 SHORT", f"Z {z:.2f} — 고평가 확인, 진입 적기", "진입"
        elif z >= z_entry:
            strength, strength_desc, action_step = "🟠 SHORT 준비", f"Z {z:.2f} — 임계값까지 {short_gap:+.2f} 남음", "준비"
        else:
            strength, strength_desc, action_step = "⚪ 중립", f"Z {z:.2f} — 중립 구간", "관망"
    else:
        if z <= -CONFIG["z_stoploss"]:
            strength, strength_desc, action_step = "🚨 위험 (스톱로스 구간)", "추세 이탈 위험 — 진입 자제", "관망"
        elif z <= -long_thresh * 1.3:
            strength, strength_desc, action_step = "🟢🟢 매우 강한 LONG", f"Z {z:.2f} — 강하게 저평가, 강진입 고려", "강진입"
        elif z <= -long_thresh:
            strength, strength_desc, action_step = "🟢 강한 LONG", f"Z {z:.2f} — 저평가 확인, 진입 적기", "진입"
        elif z <= -z_entry:
            strength, strength_desc, action_step = "🟡 LONG 준비", f"Z {z:.2f} — 임계값까지 {long_gap:+.2f} 남음", "준비"
        else:
            strength, strength_desc, action_step = "⚪ 중립", f"Z {z:.2f} — 중립 구간", "관망"

    gauge = {"관망":"⬜⬜⬜⬜ 관망","준비":"🟨⬜⬜⬜ 준비",
             "진입":"🟧🟧⬜⬜ 진입","강진입":"🟥🟥🟥⬜ 강진입"}.get(action_step,"⬜⬜⬜⬜")

    # ── 공통 데이터 준비 ─────────────────────────
    z_series = data["z_score"].dropna()
    z_arr    = z_series.values

    # ── 2. 진입 확률 (벡터 연산 — 빠른 버전) ────
    idx_arr = np.where((z_arr >= z - 0.3) & (z_arr <= z + 0.3))[0]

    win_count       = 0
    revert_days_list = []
    horizon         = 20

    for loc in idx_arr:
        future = z_arr[loc+1 : loc+horizon+1]
        if len(future) == 0:
            continue
        if z > 0:
            hit = np.where(future <= CONFIG["z_exit"])[0]
        else:
            hit = np.where(future >= -CONFIG["z_exit"])[0]
        if len(hit) > 0:
            win_count += 1
            revert_days_list.append(int(hit[0]) + 1)

    n_zone     = len(idx_arr)
    entry_prob = round(win_count / n_zone * 100, 1) if n_zone > 0 else 0.0
    prob_desc  = (
        f"{entry_prob}% — 과거 유사 Z 구간 SHORT 성공률 (표본 {n_zone}건)"
        if z > 0 else
        f"{entry_prob}% — 과거 유사 Z 구간 LONG 성공률 (표본 {n_zone}건)"
    )
    prob_bar   = "🟩" * int(entry_prob // 10) + "⬜" * (10 - int(entry_prob // 10))
    avg_days   = round(float(np.mean(revert_days_list)), 1) if revert_days_list else None
    scenario_desc = (
        f"과거 유사 구간 평균 회귀 소요: {avg_days}일\n"
        f"  → 예상 청산 시점: 진입 후 약 {avg_days}일 후"
        if avg_days else "회귀 시나리오 데이터 부족"
    )

    # ── 3. Z 백분위 ───────────────────────────────
    percentile = round(float((z_arr < z).mean() * 100), 1)
    pct_desc   = (
        f"{percentile}%ile — 상위 {100-percentile:.1f}% 고평가 (역대 최고 수준에 근접)"
        if z > 0 else
        f"{percentile}%ile — 하위 {percentile:.1f}% 저평가 (역대 최저 수준에 근접)"
    )

    # ── 4. 예상 수익률 & 목표가 ───────────────────
    gap_pct      = round((spot - fair) / spot * 100, 2)
    exp_ret_full = round(abs(spot - fair) / spot * 100, 2)
    exp_ret_half = round(exp_ret_full / 2, 2)
    target_full  = fair
    target_half  = round((spot + fair) / 2, 1)
    recent    = data["z_score"].iloc[-5:].values
    trend_bar = ""
    for v in recent:
        if v > 1.5:    trend_bar += "🔴"
        elif v > 0.5:  trend_bar += "🟠"
        elif v < -1.5: trend_bar += "🟢"
        elif v < -0.5: trend_bar += "🟡"
        else:          trend_bar += "⚪"
    z_trend = "상승📈" if recent[-1] > recent[0] else "하락📉"
    z_delta = round(recent[-1] - recent[0], 2)

    # ── 7. 변동성 ────────────────────────────────
    vol_20    = float(data["ret_usdkrw"].iloc[-20:].std() * (252**0.5) * 100)
    vol_label = "높음🔥" if vol_20 > 12 else ("보통🌤" if vol_20 > 7 else "낮음❄️")
    gap_dir   = "고평가🔴" if gap_pct > 0 else "저평가🟢"

    direction_word = "SHORT" if z > 0 else "LONG"

    return (
        f"\n══════════════════════\n"
        f"📐 진입 타이밍 가이드\n"
        f"══════════════════════\n"
        f"신호 강도 : {strength}\n"
        f"상세      : {strength_desc}\n"
        f"액션 단계 : {gauge}\n"
        f"──────────────────────\n"
        f"🎯 진입 확률\n"
        f"  {prob_bar}\n"
        f"  {prob_desc}\n"
        f"──────────────────────\n"
        f"💹 시세 분석\n"
        f"  현재환율 : {spot:.1f}원\n"
        f"  공정가   : {fair:.1f}원\n"
        f"  괴리     : {gap_pct:+.2f}% ({gap_dir})\n"
        f"  Z-score  : {z:.2f}  (최적임계 ±{z_entry})\n"
        f"──────────────────────\n"
        f"📊 과거 대비 Z 위치\n"
        f"  {pct_desc}\n"
        f"──────────────────────\n"
        f"💰 예상 수익 ({direction_word})\n"
        f"  목표① 공정가 완전수렴 : {target_full:.1f}원 → {exp_ret_full:+.2f}%\n"
        f"  목표② 절반수렴 (보수적): {target_half:.1f}원 → {exp_ret_half:+.2f}%\n"
        f"──────────────────────\n"
        f"⏱ 평균회귀 시나리오\n"
        f"  {scenario_desc}\n"
        f"──────────────────────\n"
        f"🧭 추세 분석\n"
        f"  Regime   : {regime}\n"
        f"  유효임계 : SHORT>{short_thresh} / LONG<-{long_thresh}\n"
        f"  ※ BULL추세 시 SHORT 임계값 ×1.5 상향 적용\n"
        f"──────────────────────\n"
        f"📈 최근 5일 Z 흐름\n"
        f"  {trend_bar}  ({z_trend} {z_delta:+.2f})\n"
        f"  값: {' → '.join([f'{v:.2f}' for v in recent])}\n"
        f"──────────────────────\n"
        f"📊 변동성\n"
        f"  연환산: {vol_20:.1f}%  ({vol_label})\n"
        f"──────────────────────\n"
        f"🎯 진입 시나리오\n"
        f"[SHORT] 조건: Z > {short_thresh}\n"
        f"  청산목표: Z < ±{CONFIG['z_exit']}\n"
        f"  스톱로스: Z > {CONFIG['z_stoploss']}\n"
        f"[LONG] 조건: Z < -{long_thresh}\n"
        f"  청산목표: Z > ±{CONFIG['z_exit']}\n"
        f"  스톱로스: Z < -{CONFIG['z_stoploss']}"
    )

# ─────────────────────────────────────────────
# 7. 명령어 처리
# ─────────────────────────────────────────────
def handle_command(text: str, data: pd.DataFrame, z_entry: float) -> str:
    parts = text.strip().split()
    cmd   = parts[0].lower()

    if cmd == "/long":
        if len(parts) < 2:
            return "❌ 사용법: /long [진입가격] [자본금(선택)]\n예) /long 1480.5\n예) /long 1480.5 5000000"
        try:
            price = float(parts[1])
        except ValueError:
            return "❌ 가격 형식 오류.\n예) /long 1480.5"
        capital = CONFIG["default_capital"]
        if len(parts) >= 3:
            try:
                capital = float(parts[2])
            except ValueError:
                return "❌ 자본금 형식 오류.\n예) /long 1480.5 5000000"
        pos = read_position()
        if pos["active"]:
            return f"⚠️ 이미 {pos['direction']} 포지션 활성 중.\n먼저 /exit 으로 청산하세요."
        today = datetime.date.today().strftime("%Y-%m-%d")
        write_position(True, "LONG", price, today, capital)
        return (
            f"✅ LONG 진입 선언\n──────────────────────\n"
            f"  진입가 : {price:.1f}\n  자본금 : {int(capital):,}원\n  날짜   : {today}\n\n"
            f"📌 /status 로 수익률을 확인하세요."
        )

    elif cmd == "/short":
        if len(parts) < 2:
            return "❌ 사용법: /short [진입가격] [자본금(선택)]\n예) /short 1527.6\n예) /short 1527.6 3000000"
        try:
            price = float(parts[1])
        except ValueError:
            return "❌ 가격 형식 오류.\n예) /short 1527.6"
        capital = CONFIG["default_capital"]
        if len(parts) >= 3:
            try:
                capital = float(parts[2])
            except ValueError:
                return "❌ 자본금 형식 오류.\n예) /short 1527.6 3000000"
        pos = read_position()
        if pos["active"]:
            return f"⚠️ 이미 {pos['direction']} 포지션 활성 중.\n먼저 /exit 으로 청산하세요."
        today = datetime.date.today().strftime("%Y-%m-%d")
        write_position(True, "SHORT", price, today, capital)
        return (
            f"✅ SHORT 진입 선언\n──────────────────────\n"
            f"  진입가 : {price:.1f}\n  자본금 : {int(capital):,}원\n  날짜   : {today}\n\n"
            f"📌 /status 로 수익률을 확인하세요."
        )

    elif cmd == "/exit":
        pos = read_position()
        if not pos["active"]:
            return "⚠️ 활성 포지션이 없습니다."
        pnl = calc_pnl(data, pos)
        write_position(False)
        if pnl:
            emoji = "🎉" if pnl["pnl_pct"] >= 0 else "😞"
            return (
                f"{emoji} 청산 완료\n──────────────────────\n"
                f"  방향     : {pnl['direction']}\n"
                f"  진입가   : {pnl['entry_price']:.1f}\n"
                f"  청산가   : {pnl['current_price']:.1f}\n"
                f"  보유기간 : {pnl['holding_days']}일\n"
                f"  자본금   : {int(pnl['capital']):,}원\n──────────────────────\n"
                f"  최종수익 : {pnl['pnl_pct']:+.3f}%\n"
                f"  수익금액 : {int(pnl['pnl_krw']):+,}원\n"
                f"  MFE     : {pnl['mfe_pct']:+.3f}%\n"
                f"  MAE     : {pnl['mae_pct']:+.3f}%"
            )
        return "✅ 포지션 청산 완료."

    elif cmd == "/status":
        try:
            z_entry_now = z_ref_global.get("z_entry", 1.0)
            latest = data.iloc[-1]
            z      = float(latest["z_score"])
            sig    = {1:"LONG 추천 📈", -1:"SHORT 추천 📉", 0:"NEUTRAL ⚪"}.get(
                      int(latest["sys_signal"]), "HOLD ⏸")
            part1  = (
                f"📊 [현재 상태] {_now()}\n══════════════════════\n"
                f"💰 현재환율 : {float(latest['usdkrw']):.1f}\n"
                f"⚖️ 공정가   : {float(latest['fair']):.1f}\n"
                f"📐 괴리(Z)  : {z:.2f}\n"
                f"🧭 추세     : {latest['regime']}\n"
                f"📌 시스템   : {sig}"
            )

            # Sheets 읽기 — 실패해도 기본값으로 진행
            try:
                pos = read_position()
            except Exception as e:
                print(f"[{_now()}] ⚠️ /status read_position 오류: {e}")
                pos = {"active": False, "direction": "", "entry_price": 0.0,
                       "entry_date": "", "capital": CONFIG["default_capital"]}

            if pos["active"]:
                try:
                    pnl = calc_pnl(data, pos)
                except Exception as e:
                    print(f"[{_now()}] ⚠️ calc_pnl 오류: {e}")
                    pnl = None

                if pnl:
                    emoji = "📈" if pnl["pnl_pct"] >= 0 else "📉"
                    part2 = (
                        f"\n══════════════════════\n💼 내 포지션\n"
                        f"──────────────────────\n"
                        f"  방향     : {pnl['direction']}\n"
                        f"  진입가   : {pnl['entry_price']:.1f}\n"
                        f"  현재가   : {pnl['current_price']:.1f}\n"
                        f"  보유기간 : {pnl['holding_days']}일\n"
                        f"  자본금   : {int(pnl['capital']):,}원\n"
                        f"{emoji} 수익률  : {pnl['pnl_pct']:+.3f}%\n"
                        f"💵 수익금액 : {int(pnl['pnl_krw']):+,}원\n"
                        f"──────────────────────\n"
                        f"  MFE(최대수익): {pnl['mfe_pct']:+.3f}%\n"
                        f"  MAE(최대손실): {pnl['mae_pct']:+.3f}%"
                    )
                else:
                    part2 = (
                        f"\n══════════════════════\n💼 내 포지션\n"
                        f"──────────────────────\n"
                        f"  방향   : {pos['direction']}\n"
                        f"  진입가 : {pos['entry_price']}\n"
                        f"  날짜   : {pos['entry_date']}\n"
                        f"  ⚠️ 수익률 계산 불가 (진입일 데이터 없음)"
                    )
            else:
                try:
                    part2 = fmt_entry_guide(data, z_entry_now)
                except Exception as e:
                    print(f"[{_now()}] ⚠️ fmt_entry_guide 오류: {e}")
                    part2 = f"\n──────────────────────\n⏳ 포지션 미진입 (관망 중)\n오류: {e}"

            print(f"[{_now()}] ✅ /status 응답 완료")
            return part1 + part2

        except Exception as e:
            print(f"[{_now()}] ❌ /status 전체 오류: {e}")
            return f"⚠️ /status 처리 오류: {e}"

    else:
        return (
            "📋 사용 가능한 명령어\n──────────────────────\n"
            "  /long [가격] [자본금]  — 롱 진입\n"
            "  /short [가격] [자본금] — 숏 진입\n"
            "  /exit                  — 청산\n"
            "  /status                — 현재 상태 조회\n\n"
            f"※ 자본금 생략 시 기본값: {CONFIG['default_capital']:,}원"
        )

# ─────────────────────────────────────────────
# 8. 스톱로스 근접 경고
# ─────────────────────────────────────────────
_warning_sent = {"SHORT": False, "LONG": False}

def check_stoploss_warning(data: pd.DataFrame, pos: dict) -> None:
    if not pos["active"]:
        _warning_sent["SHORT"] = False
        _warning_sent["LONG"]  = False
        return

    z         = float(data["z_score"].iloc[-1])
    threshold = CONFIG["z_stoploss"] * CONFIG["z_warning_ratio"]
    direction = pos["direction"]

    if direction == "SHORT" and z >= threshold and not _warning_sent["SHORT"]:
        _warning_sent["SHORT"] = True
        pnl = calc_pnl(data, pos)
        pnl_line = f"현재 손실: {pnl['pnl_pct']:+.3f}% ({int(pnl['pnl_krw']):+,}원)" if pnl else ""
        send_telegram(
            f"⚠️ 스톱로스 근접 경고!\n──────────────────────\n"
            f"  포지션  : SHORT\n  현재 Z  : {z:.2f}\n"
            f"  경고기준: {threshold:.2f} / 스톱로스: {CONFIG['z_stoploss']}\n"
            f"  {pnl_line}\n\n⚡ 청산 고려: /exit"
        )
    elif direction == "LONG" and z <= -threshold and not _warning_sent["LONG"]:
        _warning_sent["LONG"] = True
        pnl = calc_pnl(data, pos)
        pnl_line = f"현재 손실: {pnl['pnl_pct']:+.3f}% ({int(pnl['pnl_krw']):+,}원)" if pnl else ""
        send_telegram(
            f"⚠️ 스톱로스 근접 경고!\n──────────────────────\n"
            f"  포지션  : LONG\n  현재 Z  : {z:.2f}\n"
            f"  경고기준: -{threshold:.2f} / 스톱로스: -{CONFIG['z_stoploss']}\n"
            f"  {pnl_line}\n\n⚡ 청산 고려: /exit"
        )
    elif direction == "SHORT" and z < threshold:
        _warning_sent["SHORT"] = False
    elif direction == "LONG"  and z > -threshold:
        _warning_sent["LONG"]  = False

# ─────────────────────────────────────────────
# 9. 청산 추천 자동 알림
# ─────────────────────────────────────────────
_exit_alerted = False

def check_exit_signal(data: pd.DataFrame, pos: dict) -> None:
    global _exit_alerted
    if not pos["active"] or _exit_alerted:
        if not pos["active"]:
            _exit_alerted = False
        return

    latest     = data.iloc[-1]
    sys_action = latest["sys_action"]
    z          = float(latest["z_score"])

    if sys_action not in ("EXIT", "STOP_LOSS_EXIT"):
        return

    pnl      = calc_pnl(data, pos)
    pnl_line = (f"  현재 수익 : {pnl['pnl_pct']:+.3f}% ({int(pnl['pnl_krw']):+,}원)"
                if pnl else "")
    reason   = {
        "EXIT"          : f"Z-score({z:.2f})가 ±{CONFIG['z_exit']} 이내 → 괴리 해소",
        "STOP_LOSS_EXIT": f"Z-score({z:.2f})가 ±{CONFIG['z_stoploss']} 초과 → 추세 지속",
    }.get(sys_action, "")

    send_telegram(
        f"🚨 청산 추천 알림\n──────────────────────\n"
        f"  포지션 : {pos['direction']}\n"
        f"  신호   : {sys_action}\n"
        f"  이유   : {reason}\n"
        f"{pnl_line}\n\n⚡ /exit  |  확인: /status"
    )
    _exit_alerted = True

# ─────────────────────────────────────────────
# 10. 일간 리포트
# ─────────────────────────────────────────────
def daily_report(data_ref: dict, z_ref: dict) -> None:
    # 공유 변수 선언 (두 블록에서 모두 참조)
    data    = None
    z_entry = 1.0
    pos     = {"active": False}
    pnl     = None
    latest  = None

    # ── 데이터 준비 ──────────────────────────
    try:
        data    = data_ref["data"]
        z_entry = z_ref.get("z_entry", 1.0)
        pos     = read_position()
        pnl     = calc_pnl(data, pos)
        latest  = data.iloc[-1]
    except Exception as e:
        print(f"[{_now()}] ❌ 데이터 준비 오류: {e}")
        send_telegram(f"⚠️ 일간 리포트 데이터 오류\n{e}")
        return  # 데이터 없으면 이후 진행 불가

    # ── 텔레그램 발송 ────────────────────────
    try:
        sig   = {1:"LONG 추천 📈", -1:"SHORT 추천 📉", 0:"NEUTRAL ⚪"}.get(
                 int(latest["sys_signal"]), "HOLD ⏸")
        part1 = (
            f"📊 [USD/KRW 일간 리포트] {_now()}\n══════════════════════\n"
            f"💰 현재환율 : {float(latest['usdkrw']):.1f}\n"
            f"⚖️ 공정가   : {float(latest['fair']):.1f}\n"
            f"📐 괴리(Z)  : {float(latest['z_score']):.2f}\n"
            f"🧭 추세     : {latest['regime']}\n"
            f"📌 시스템   : {sig}"
        )
        if pos["active"] and pnl:
            emoji = "📈" if pnl["pnl_pct"] >= 0 else "📉"
            part2 = (
                f"\n══════════════════════\n💼 내 포지션 현황\n══════════════════════\n"
                f"  방향     : {pnl['direction']}\n"
                f"  진입가   : {pnl['entry_price']:.1f}\n"
                f"  현재가   : {pnl['current_price']:.1f}\n"
                f"  보유기간 : {pnl['holding_days']}일\n"
                f"  자본금   : {int(pnl['capital']):,}원\n"
                f"{emoji} 수익률  : {pnl['pnl_pct']:+.3f}%\n"
                f"💵 수익금액 : {int(pnl['pnl_krw']):+,}원\n──────────────────────\n"
                f"  MFE(최대수익): {pnl['mfe_pct']:+.3f}%  ({int(pnl['mfe_krw']):+,}원)\n"
                f"  MAE(최대손실): {pnl['mae_pct']:+.3f}%  ({int(pnl['mae_krw']):+,}원)\n"
                f"──────────────────────\n"
                f"🎯 청산 목표: Z < ±{CONFIG['z_exit']}\n"
                f"🛑 스톱로스 : Z > ±{CONFIG['z_stoploss']}"
            )
        else:
            part2 = fmt_entry_guide(data, z_entry)

        send_telegram(part1 + part2)
        print(f"[{_now()}] ✅ 일간 리포트 텔레그램 발송 완료")
    except Exception as e:
        print(f"[{_now()}] ❌ 텔레그램 발송 오류: {e}")
        send_telegram(f"⚠️ 리포트 발송 오류\n{e}")

    # ── Google Sheets 로그 (실패해도 텔레그램과 무관) ──
    try:
        pr = pnl or {}
        append_log([
            str(latest.name.date()),
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            round(float(latest["usdkrw"]), 2),
            round(float(latest["fair"]), 2),
            round(float(latest["z_score"]), 4),
            int(latest["sys_signal"]),
            latest["sys_action"],
            latest["regime"],
            pos["active"],
            pr.get("direction",""),
            pr.get("entry_price",""),
            str(pr.get("entry_date","").date()) if pr.get("entry_date") else "",
            pr.get("current_price",""),
            pr.get("holding_days",""),
            pr.get("pnl_pct",""),
            pr.get("pnl_krw",""),
            pr.get("mfe_pct",""),
            pr.get("mae_pct",""),
        ])
        print(f"[{_now()}] ✅ Sheets 로그 저장 완료")
    except Exception as e:
        print(f"[{_now()}] ⚠️ Sheets 저장 오류 (텔레그램 발송은 완료): {e}")

# ─────────────────────────────────────────────
# 11. MAIN LOOP
# ─────────────────────────────────────────────
z_ref_global = {}  # /status 명령어에서 참조용

def main():
    print("=" * 55)
    print("  USD/KRW Quant Trading System v5.0 (Railway)")
    print("=" * 55)

    print("\n📡 초기 데이터 로딩 중...")
    data_ref = {}
    refresh_data(data_ref, z_ref_global)

    schedule.every(CONFIG["data_refresh_min"]).minutes.do(
        refresh_data, data_ref, z_ref_global)
    schedule.every().day.at(CONFIG["daily_report_time"]).do(
        daily_report, data_ref, z_ref_global)

    send_telegram(
        "🤖 USD/KRW Quant Bot v5 시작! (Railway)\n\n"
        "📋 명령어\n──────────────────────\n"
        "  /long [가격] [자본금]  — 롱 진입\n"
        "  /short [가격] [자본금] — 숏 진입\n"
        "  /exit                  — 청산\n"
        "  /status                — 현재 상태\n\n"
        "🔔 자동 알림\n"
        "  • 매일 09:00 KST 상세 일간 리포트\n"
        "  • 스톱로스 90% 근접 시 경고\n"
        "  • 시스템 청산 신호 감지 시 추천"
    )
    print(f"✅ 봇 시작 완료. 일간 리포트: 매일 09:00 KST (UTC 00:00)\n")

    # 시작 직후 즉시 리포트 1회 발송 (정상 작동 확인용)
    daily_report(data_ref, z_ref_global)

    my_chat_id = str(CONFIG["telegram_chat_id"])

    while True:
        try:
            schedule.run_pending()

            pos = read_position()
            if pos["active"] and "data" in data_ref:
                check_stoploss_warning(data_ref["data"], pos)
                check_exit_signal(data_ref["data"], pos)
            elif not pos["active"]:
                global _exit_alerted
                _exit_alerted = False

            for upd in get_updates():
                msg = upd.get("message", {})
                if not msg:
                    continue
                if str(msg.get("chat",{}).get("id","")) != my_chat_id:
                    continue
                text = msg.get("text","").strip()
                if not text.startswith("/"):
                    continue

                print(f"[{_now()}] 📨 {text}")
                if text.lower().startswith(("/long","/short","/exit")):
                    _warning_sent["SHORT"] = False
                    _warning_sent["LONG"]  = False
                    _exit_alerted = False

                if "data" not in data_ref:
                    send_telegram("⚠️ 데이터 로딩 중입니다. 잠시 후 다시 시도해 주세요.")
                    continue

                try:
                    reply = handle_command(text, data_ref["data"], z_ref_global.get("z_entry", 1.0))
                except Exception as e:
                    reply = f"⚠️ 명령어 처리 오류: {e}"
                    print(f"[{_now()}] ❌ 명령어 오류 ({text}): {e}")
                send_telegram(reply)

        except KeyboardInterrupt:
            print("\n⛔ 봇 종료.")
            send_telegram("⛔ USD/KRW Quant Bot 종료.")
            break
        except Exception as e:
            print(f"루프 오류: {e}")

        time.sleep(CONFIG["poll_interval_sec"])

if __name__ == "__main__":
    main()
