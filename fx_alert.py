#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
USD/KRW 환율 알림.

두 가지 알림을 동시에 처리한다.
  A. 절대 격자: 환율이 5원 격자(1450, 1455, 1460 ...)를 통과할 때마다 알림.
  B. 9시 기준: 매일 오전 9시(KST) 환율을 기준값으로 잡고, 거기서 ±5원 단위로 움직일 때마다 알림.

상태는 state.json 한 파일에만 저장한다. 격자 단계가 실제로 바뀔 때만 값이 변하므로
GitHub Actions가 state.json을 커밋하는 횟수는 하루 몇 번 수준이다.

환경변수
  TELEGRAM_BOT_TOKEN  텔레그램 봇 토큰 (필수, 시크릿)
  TELEGRAM_CHAT_ID    수신할 chat_id (필수, 시크릿)
  DRY_RUN=1           전송 대신 화면 출력 (로컬 테스트용)
  TEST_RATE=1455.0    네트워크 호출 없이 지정 환율로 동작 (로컬 테스트용)
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ──────────────────────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────────────────────
ENABLE_ABSOLUTE_GRID = True   # 버전 A 사용 여부
ENABLE_BASELINE_GRID = True   # 버전 B 사용 여부
GRID_STEP = 5                 # 격자/단위 (원). 10으로 바꾸면 10원 단위 알림
SEND_DAILY_BASELINE_MSG = True  # 매일 9시 기준값 안내 메시지 전송 여부
RESET_HOUR_KST = 9            # 기준값을 새로 잡는 시각 (KST)

# 야간 무음 시간대. 비우면 24시간 알림. 예: QUIET_HOURS = (0, 7) → 00~07시 알림 억제
QUIET_HOURS = None            # (시작시각, 종료시각) 또는 None

KST = timezone(timedelta(hours=9))
STATE_FILE = Path(__file__).with_name("state.json")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


# ──────────────────────────────────────────────────────────────
# 환율 수집 (출처 우선순위: 야후 → 네이버 → er-api)
#   야후 KRW=X 와 네이버 매매기준율은 몇 원 차이가 날 수 있다.
#   '네이버에 뜨는 숫자'를 쓰고 싶으면 SOURCES 순서를 (fetch_naver, fetch_yahoo, ...)로 바꾼다.
# ──────────────────────────────────────────────────────────────
def fetch_yahoo():
    url = "https://query1.finance.yahoo.com/v8/finance/chart/KRW=X"
    r = requests.get(url, headers=UA, timeout=10)
    r.raise_for_status()
    meta = r.json()["chart"]["result"][0]["meta"]
    return float(meta["regularMarketPrice"])


def fetch_naver():
    # 네이버페이 증권 시장지표 페이지의 첫 환율(USD 매매기준율)을 읽는다.
    from bs4 import BeautifulSoup
    url = "https://finance.naver.com/marketindex/"
    r = requests.get(url, headers=UA, timeout=10)
    r.raise_for_status()
    r.encoding = "euc-kr"
    soup = BeautifulSoup(r.text, "html.parser")
    el = soup.select_one("#exchangeList .value")
    if el is None:
        raise ValueError("네이버 환율 요소를 찾지 못함 (페이지 구조 변경 가능)")
    return float(el.get_text(strip=True).replace(",", ""))


def fetch_erapi():
    # 일 1회 갱신. 장중 정밀도는 없으나 위 두 곳이 모두 실패할 때 스크립트를 살려둔다.
    url = "https://open.er-api.com/v6/latest/USD"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return float(r.json()["rates"]["KRW"])


SOURCES = (fetch_yahoo, fetch_naver, fetch_erapi)


def get_rate():
    """현재 USD/KRW를 (환율, 출처)로 반환한다. TEST_RATE가 있으면 그 값을 쓴다."""
    test = os.environ.get("TEST_RATE")
    if test:
        return float(test), "test"

    last_err = None
    for fn in SOURCES:
        try:
            rate = fn()
            if rate and 500 < rate < 5000:   # 비정상 값 방어
                return rate, fn.__name__.replace("fetch_", "")
            last_err = f"{fn.__name__}: 범위 밖 값 {rate}"
        except Exception as e:            # 출처별 실패는 로깅 후 다음 출처로
            last_err = f"{fn.__name__}: {e}"
            print(f"[warn] {last_err}")
    raise RuntimeError(f"모든 환율 출처 실패. 마지막 오류: {last_err}")


# ──────────────────────────────────────────────────────────────
# 상태 입출력
# ──────────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[warn] state.json 읽기 실패, 초기화: {e}")
    return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


# ──────────────────────────────────────────────────────────────
# 격자 계산
# ──────────────────────────────────────────────────────────────
def abs_band(rate):
    """절대 격자 인덱스. 경계선은 정확히 5의 배수에 위치."""
    return int(rate // GRID_STEP)


def rel_level(rate, baseline):
    """기준값 대비 부호 있는 단계. 위/아래 모두 'GRID_STEP만큼 움직였을 때' 1단계 증가."""
    diff = rate - baseline
    if diff >= 0:
        return int(diff // GRID_STEP)
    return -int((-diff) // GRID_STEP)


def window_info(now):
    """현재 시각이 속한 '9시~다음날 9시' 구간의 시작 시각과 식별자."""
    reset_today = now.replace(hour=RESET_HOUR_KST, minute=0, second=0, microsecond=0)
    start = reset_today if now >= reset_today else reset_today - timedelta(days=1)
    return start, start.strftime("%Y-%m-%d")


# ──────────────────────────────────────────────────────────────
# 텔레그램 전송
# ──────────────────────────────────────────────────────────────
def send_telegram(text):
    if os.environ.get("DRY_RUN") == "1":
        print("[DRY_RUN] 전송할 메시지:\n" + text)
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수가 없음")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=10,
    )
    # 토큰이 URL에 들어가므로 실패 시 토큰이 로그에 남지 않도록 상태코드만 출력
    if not r.ok:
        raise RuntimeError(f"텔레그램 전송 실패: HTTP {r.status_code}")


# ──────────────────────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────────────────────
def main():
    # 로컬에서 'python fx_alert.py test' 로 전송 경로만 점검
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        send_telegram("환율 알림 봇 연결 테스트. 이 메시지가 보이면 설정 완료.")
        print("테스트 메시지 전송 시도 완료.")
        return

    now = datetime.now(KST)
    rate, source = get_rate()
    state = load_state()
    print(f"[info] {now:%Y-%m-%d %H:%M} KST  rate={rate}  source={source}")

    win_start, win_id = window_info(now)

    # 9시 구간이 바뀌면 기준값을 새로 잡는다
    baseline_reset = False
    if state.get("baseline_window") != win_id:
        state["baseline"] = rate
        state["baseline_window"] = win_id
        state["last_rel_level"] = 0
        baseline_reset = True

    lines = []

    # ── 기준값 안내 메시지 ──
    if baseline_reset and SEND_DAILY_BASELINE_MSG:
        if abs((now - win_start).total_seconds()) <= 15 * 60:
            label = f"{win_id} 오전 9시 기준"
        else:
            label = f"첫 가동 기준 ({now:%H:%M})"
        lines.append(f"[기준설정] {label}\nUSD/KRW {rate:,.2f}원")

    # ── 버전 A: 절대 5원 격자 ──
    if ENABLE_ABSOLUTE_GRID:
        curr = abs_band(rate)
        prev = state.get("last_band_abs")
        if prev is not None and curr != prev:
            steps = curr - prev
            if steps > 0:
                line_won = curr * GRID_STEP
                tail = f" ({steps}단계)" if steps > 1 else ""
                lines.append(f"[절대격자] ▲ {line_won:,}원 상향 돌파{tail}")
            else:
                line_won = (curr + 1) * GRID_STEP
                tail = f" ({-steps}단계)" if -steps > 1 else ""
                lines.append(f"[절대격자] ▼ {line_won:,}원 하향 이탈{tail}")
        state["last_band_abs"] = curr

    # ── 버전 B: 9시 기준 ±5원 ──
    if ENABLE_BASELINE_GRID and not baseline_reset:
        baseline = state.get("baseline")
        if baseline is not None:
            level = rel_level(rate, baseline)
            prev = state.get("last_rel_level", 0)
            if level != prev:
                delta = level * GRID_STEP
                arrow = "▲" if level > prev else "▼"
                if delta == 0:
                    body = f"기준 {baseline:,.0f}원 부근 복귀"
                else:
                    sign = "+" if delta > 0 else "-"
                    body = f"기준 {baseline:,.0f}원 대비 {sign}{abs(delta)}원"
                lines.append(f"[9시기준] {arrow} {body}")
            state["last_rel_level"] = level

    # ── 야간 무음 ──
    muted = False
    if QUIET_HOURS:
        s, e = QUIET_HOURS
        h = now.hour
        muted = (s <= h < e) if s <= e else (h >= s or h < e)

    # ── 전송 ──
    if lines and not muted:
        header = f"USD/KRW {rate:,.2f}원 · {now:%H:%M} KST · {source}"
        send_telegram(header + "\n" + "\n".join(lines))
        print("[info] 알림 전송:\n" + "\n".join(lines))
    elif lines and muted:
        print(f"[info] 무음 시간대({QUIET_HOURS}) — 전송 생략: {lines}")
    else:
        print("[info] 변동 없음 — 전송 안 함")

    save_state(state)


if __name__ == "__main__":
    main()
