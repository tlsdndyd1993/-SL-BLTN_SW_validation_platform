# =============================================================================
#  T/C 스크립트: 블랙아웃(Flicker) 유무 검증  v5
#  ─────────────────────────────────────────────────────────────────────────────
#  실행 흐름
#   [사전] 초기화 (블랙아웃 기능 OFF → 카운트 기준점 스냅샷)
#   [1] 10초 대기
#   [2] B+ OFF
#   [3] 10초 대기
#   [4] B+ ON
#   [5-A] 블랙아웃 기능 ON (Key ON 직후부터 감지 필요)
#   [5-B] 검정화면 전환 확인 — 10초 이내 카운트 +1 이상?
#          NO  → 녹색 버그(검정 미전환) → FAIL + 30초 대기 + 종료
#          YES → 정상 전환 확인 완료 → Step 6 진행
#   [6] 정상화면 ROI 밝기 감시 — 10초 이내 목표(220) 이상?
#          NO  → FAIL + 수동녹화 + 30초 대기 + 종료
#          YES → Step 7 진행
#   [7] 20초 Flicker 감시
#          카운트 기준+2 이상 → FAIL + 20초 대기 + 블랙아웃 OFF + 종료
#          없음              → PASS + 블랙아웃 OFF + 종료
#
#  카운트 흐름 정리:
#    Step5-B에서 기준점 대비 +1 확인 (녹색→검정 정상 전환)
#    Step7에서 기준점 대비 +2 이상 시 FAIL (정상화면→검정 추가 전환 = Flicker)
# =============================================================================

# ── 파라미터 ──────────────────────────────────────────────────────────────────
ROI_BRIGHTNESS_TARGET = 220.0   # 카메라 ROI 평균 밝기 임계값 (0~255)
BLACKOUT_WAIT_SEC     = 10.0    # Key ON 후 검정 전환 확인 제한 시간 (초)
KEY_ON_TIMEOUT        = 10.0    # 검정 전환 후 정상화면 확인 제한 시간 (초)
FLICKER_WATCH_SEC     = 20.0    # Flicker 감시 구간 (초)
FAIL_WAIT_SEC         = 20.0    # Flicker FAIL 후 추가 대기 (초)
ROI_FAIL_WAIT_SEC     = 30.0    # 각종 FAIL 후 대기 (초)
MANUAL_PRE_SEC        = 20.0    # 수동녹화 앞 구간 (초)
MANUAL_POST_SEC       = 30.0    # 수동녹화 뒤 구간 (초)

# 초록→검정(+1회)은 정상, 정상화면→검정(+2회)부터 Flicker FAIL
BO_FAIL_THRESHOLD = 2

POLL_INTERVAL = 0.1   # 감지 체크 주기 (초)
EMIT_INTERVAL = 0.5   # 상태바 갱신 주기 (초) — Qt 부하 절감


# ── 헬퍼 함수 ─────────────────────────────────────────────────────────────────
def _cam_brightness():
    ov = getattr(engine, 'camera_overall_avg', None)
    if ov is None:
        return 0.0
    try:
        b, g, r = float(ov[0]), float(ov[1]), float(ov[2])
        return 0.114 * b + 0.587 * g + 0.299 * r
    except Exception:
        return 0.0

def _cam_bo_count():
    return int(getattr(engine, 'camera_bo_count', 0))

def _wait_with_status(total_sec, label):
    """중단 감지 + 상태바 카운트다운."""
    t0        = time.perf_counter()
    last_emit = -EMIT_INTERVAL
    while not kernel.is_stopped():
        elapsed = time.perf_counter() - t0
        if elapsed >= total_sec:
            break
        if elapsed - last_emit >= EMIT_INTERVAL:
            kernel.emit_status(f"[TC] {label}  ({elapsed:.1f}s / {total_sec:.0f}s)")
            last_emit = elapsed
        kernel.wait(POLL_INTERVAL)


# =============================================================================
#  [사전] 초기화
#  ★ 블랙아웃 기능은 Step5-A에서 ON — Key ON 직후 검정 전환을 감지해야 함
#  ★ 기준 카운트는 여기서 스냅샷 (B+ OFF 상태의 값)
# =============================================================================
log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
log("[INIT] 블랙아웃 Flicker 검증 스크립트 시작")
log(f"[INIT] 파라미터: ROI목표={ROI_BRIGHTNESS_TARGET}  "
    f"검정전환확인={BLACKOUT_WAIT_SEC}s  정상화면확인={KEY_ON_TIMEOUT}s  "
    f"감시구간={FLICKER_WATCH_SEC}s  FAIL임계=+{BO_FAIL_THRESHOLD}회")
kernel.emit_status("[TC] 초기화 중...")

engine.blackout_rec_enabled = False   # 아직 OFF — Step5-A에서 ON
_bo_ref = _cam_bo_count()             # ★ B+ OFF 상태 기준점 저장

if kernel.is_stopped():
    log("[ABORT] 시작 전 중단 감지 — 종료"); raise SystemExit


# =============================================================================
#  [1] 10초 대기
# =============================================================================
log("")
log("[STEP 1/7] 10초 대기")
_wait_with_status(10.0, "Step1 — 10초 대기")
log("[STEP 1/7] 완료")
if kernel.is_stopped():
    log("[ABORT] Step1 후 중단"); raise SystemExit


# =============================================================================
#  [2] B+ OFF
# =============================================================================
log("")
log("[STEP 2/7] B+ OFF (BLTN B+ OFF)")
kernel.emit_status("[TC] Step2 — BLTN B+ OFF")
kernel.power_off("bplus")
kernel.wait(0.1)
log("[STEP 2/7] B+ OFF 완료")
if kernel.is_stopped():
    log("[ABORT] Step2 후 중단"); raise SystemExit


# =============================================================================
#  [3] 10초 대기
# =============================================================================
log("")
log("[STEP 3/7] 10초 대기")
_wait_with_status(10.0, "Step3 — 10초 대기 (B+ OFF 안정화)")
log("[STEP 3/7] 완료")
if kernel.is_stopped():
    log("[ABORT] Step3 후 중단"); raise SystemExit


# =============================================================================
#  [4] B+ ON
# =============================================================================
log("")
log("[STEP 4/7] B+ ON")
kernel.emit_status("[TC] Step4 — BLTN B+ ON")
kernel.power_on("bplus");    kernel.wait(0.1)
kernel.power_on("tg_bplus"); kernel.wait(0.1)
kernel.power_on("acc");      kernel.wait(0.1)
kernel.power_on("ign");      kernel.wait(0.1)
log("[STEP 4/7] B+ ON 완료")
if kernel.is_stopped():
    log("[ABORT] Step4 후 중단"); raise SystemExit


# =============================================================================
#  [5-A] 블랙아웃 기능 ON
#  Key ON 직후부터 감지해야 녹색→검정 전환을 카운트할 수 있음
# =============================================================================
log("")
log("[STEP 5-A] 블랙아웃 감지 ON (카운트만 — 클립 녹화 없음)")
# ★ 사이클마다 카운트 0 초기화
engine.camera_bo_count    = 0
engine.screen_bo_count    = 0
_bo_ref = 0
# ★ count_only=True: 녹색→검정 전환은 카운트+로그만, 클립 저장 안 함
#    정상화면 진입 후 Step7에서 False로 전환해야 클립 녹화 활성화
engine.blackout_count_only  = True
engine.blackout_rec_enabled = True
log("[STEP 5-A] 블랙아웃 ON (count_only 모드) — 카운트 초기화(0) 완료")

if kernel.is_stopped():
    log("[ABORT] Step5-A 후 중단")
    engine.blackout_rec_enabled = False
    raise SystemExit


# =============================================================================
#  [5-B] 검정화면 전환 확인 — BLACKOUT_WAIT_SEC(10초) 이내 카운트 +1 필수
#
#  정상: 녹색 → 검정(카운트+1) → 정상화면
#  버그: 녹색이 계속 유지 → 검정 전환 없음 → FAIL
#
#  ★ 이 단계에서 카운트가 +1 오르면 "녹색→검정 전환 확인 완료"
#     이후 Step6에서 정상화면 밝기(220 이상)를 별도로 확인
# =============================================================================
log("")
log(f"[STEP 5-B] 검정화면 전환 확인  "
    f"제한={BLACKOUT_WAIT_SEC}s  기준 카운트={_bo_ref}")
kernel.emit_status(f"[TC] Step5-B — 검정화면 전환 확인 ({BLACKOUT_WAIT_SEC:.0f}초)")

_blackout_ok = False
_t0          = time.perf_counter()
_last_emit   = -EMIT_INTERVAL

while not kernel.is_stopped():
    _elapsed  = time.perf_counter() - _t0
    _bo_now   = _cam_bo_count()
    _bo_delta = _bo_now - _bo_ref

    # ── 검정 전환 감지 (+1 이상) ──────────────────────────────────────────
    if _bo_delta >= 1:
        log(f"[STEP 5-B] ✅ 검정화면 전환 확인  "
            f"카운트 {_bo_ref} → {_bo_now} (+{_bo_delta}회)  "
            f"경과={_elapsed:.2f}초")
        _blackout_ok = True
        break

    if _elapsed >= BLACKOUT_WAIT_SEC:
        log(f"[STEP 5-B] ⏱ 타임아웃 — {BLACKOUT_WAIT_SEC}초 내 검정 전환 없음  "
            f"카운트={_bo_now} (기준={_bo_ref}, 변화 없음)")
        break

    if _elapsed - _last_emit >= EMIT_INTERVAL:
        _bri = _cam_brightness()
        kernel.emit_status(
            f"[TC] Step5-B — 검정 전환 대기  "
            f"BO카운트={_bo_now}(+{_bo_delta})  밝기={_bri:.1f}  "
            f"({_elapsed:.1f}s / {BLACKOUT_WAIT_SEC:.0f}s)"
        )
        _last_emit = _elapsed

    kernel.wait(POLL_INTERVAL)

# ── 검정 전환 없음 → 녹색 버그 → FAIL ────────────────────────────────────
if not _blackout_ok:
    log("")
    log("[STEP 5-B] ❌ FAIL — 검정화면 전환 미감지 (녹색화면 유지 버그)")
    kernel.emit_status("[TC] FAIL — 검정 전환 없음, 수동녹화 중...")
    engine.blackout_rec_enabled = False   # ★ FAIL 즉시 OFF
    kernel.set_tc_result("FAIL_STEP5-B")
    engine.set_manual_source("both")
    engine.set_manual_time(MANUAL_PRE_SEC, MANUAL_POST_SEC)
    engine.save_manual_clip()
    kernel.wait_manual_clip(timeout=MANUAL_POST_SEC + 15.0)
    log(f"[STEP 5-B] 수동녹화 완료. {ROI_FAIL_WAIT_SEC:.0f}초 대기...")
    _wait_with_status(ROI_FAIL_WAIT_SEC, "FAIL 대기 (검정 전환 없음)")
    engine.blackout_rec_enabled = False
    log("[STEP 5-B] 블랙아웃 기능 비활성화 — 스크립트 종료 (FAIL_STEP5-B)")
    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    raise SystemExit


# =============================================================================
#  [6] 정상화면 ROI 밝기 감시 — KEY_ON_TIMEOUT(10초) 이내 220 이상
#  검정→정상화면 전환 후 밝기가 임계값 이상 오르는지 확인
# =============================================================================
log("")
log(f"[STEP 6/7] 정상화면 밝기 감시  "
    f"목표={ROI_BRIGHTNESS_TARGET}  제한={KEY_ON_TIMEOUT}s")
kernel.emit_status(f"[TC] Step6 — 정상화면 밝기 감시 ({KEY_ON_TIMEOUT:.0f}초)")

_roi_ok    = False
_t0        = time.perf_counter()
_last_emit = -EMIT_INTERVAL 

while not kernel.is_stopped():
    _elapsed = time.perf_counter() - _t0
    _bri     = _cam_brightness()

    if _bri >= ROI_BRIGHTNESS_TARGET:
        log(f"[STEP 6/7] ✅ ROI 밝기 {_bri:.1f} ≥ {ROI_BRIGHTNESS_TARGET}  "
            f"경과={_elapsed:.2f}초")
        _roi_ok = True
        break

    if _elapsed >= KEY_ON_TIMEOUT:
        log(f"[STEP 6/7] ⏱ 타임아웃 — {KEY_ON_TIMEOUT}초 초과  "
            f"마지막 밝기={_bri:.1f}")
        break

    if _elapsed - _last_emit >= EMIT_INTERVAL:
        kernel.emit_status(
            f"[TC] Step6 — 정상화면 밝기 감시  "
            f"현재={_bri:.1f}  목표≥{ROI_BRIGHTNESS_TARGET}  "
            f"({_elapsed:.1f}s / {KEY_ON_TIMEOUT:.0f}s)"
        )
        _last_emit = _elapsed

    kernel.wait(POLL_INTERVAL)

# ── 정상화면 미도달 → FAIL ────────────────────────────────────────────────
if not _roi_ok:
    log("")
    log("[STEP 6/7] ❌ FAIL — 정상화면 밝기 미달")
    kernel.emit_status("[TC] FAIL — 정상화면 미도달, 수동녹화 중...")
    engine.blackout_rec_enabled = False   # ★ FAIL 즉시 OFF
    kernel.set_tc_result("FAIL_STEP6")
    engine.set_manual_source("both")
    engine.set_manual_time(MANUAL_PRE_SEC, MANUAL_POST_SEC)
    engine.save_manual_clip()
    kernel.wait_manual_clip(timeout=MANUAL_POST_SEC + 15.0)
    log(f"[STEP 6/7] 수동녹화 완료. {ROI_FAIL_WAIT_SEC:.0f}초 대기...")
    _wait_with_status(ROI_FAIL_WAIT_SEC, "FAIL 대기 (정상화면 미도달)")
    engine.blackout_rec_enabled = False
    log("[STEP 6/7] 블랙아웃 기능 비활성화 — 스크립트 종료 (FAIL_STEP6)")
    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    raise SystemExit


# =============================================================================
#  [7] 20초 Flicker 감시
#  기준점 대비 +2 이상 = 정상화면 이후 추가 검정 전환 = Flicker
#  (+1은 Step5-B에서 이미 확인된 녹색→검정 전환)
# =============================================================================
log("")
# ★ 정상화면 확인 완료 → 이제부터 블랙아웃 = 진짜 Flicker → 클립 녹화 활성화
engine.blackout_count_only = False
log("[STEP 7/7] 블랙아웃 클립 녹화 활성화 (count_only → 녹화 모드)")

log(f"[STEP 7/7] Flicker 감시 시작 ({FLICKER_WATCH_SEC:.0f}초)  "
    f"FAIL 조건: 기준 대비 +{BO_FAIL_THRESHOLD}회 이상")
kernel.emit_status(f"[TC] Step7 — Flicker 감시 ({FLICKER_WATCH_SEC:.0f}초)")

_flicker   = False
_t0        = time.perf_counter()
_last_emit = -EMIT_INTERVAL

while not kernel.is_stopped():
    _elapsed  = time.perf_counter() - _t0

    if _elapsed >= FLICKER_WATCH_SEC:
        break

    _bo_now   = _cam_bo_count()
    _bo_delta = _bo_now - _bo_ref

    if _bo_delta >= BO_FAIL_THRESHOLD:
        log(f"[STEP 7/7] ⚡ Flicker 감지!  "
            f"카운트 기준={_bo_ref} 현재={_bo_now} (+{_bo_delta}회)  "
            f"경과={_elapsed:.2f}초")
        _flicker = True
        break

    if _elapsed - _last_emit >= EMIT_INTERVAL:
        _bri = _cam_brightness()
        kernel.emit_status(
            f"[TC] Step7 — Flicker 감시  "
            f"밝기={_bri:.1f}  BO카운트={_bo_now}(+{_bo_delta})  "
            f"({_elapsed:.1f}s / {FLICKER_WATCH_SEC:.0f}s)"
        )
        _last_emit = _elapsed

    kernel.wait(POLL_INTERVAL)


# =============================================================================
#  결과 처리
# =============================================================================
log("")

if _flicker:
    log("[결과] ❌ FAIL — Flicker 발생")
    kernel.emit_status(f"[TC] FAIL — Flicker 발생, {FAIL_WAIT_SEC:.0f}초 대기...")
    kernel.set_tc_result("FAIL_STEP7")
    _wait_with_status(FAIL_WAIT_SEC, "FAIL 대기 (Flicker)")
    engine.blackout_count_only  = False
    engine.blackout_rec_enabled = False
    log("[결과] 블랙아웃 기능 비활성화 — 스크립트 종료 (FAIL_STEP7)")
else:
    log(f"[결과] ✅ PASS — {FLICKER_WATCH_SEC:.0f}초간 Flicker 없음")
    kernel.emit_status("[TC] ✅ PASS — Flicker 없음")
    kernel.set_tc_result("PASS")
    engine.blackout_count_only  = False
    engine.blackout_rec_enabled = False
    log("[결과] 블랙아웃 기능 비활성화 — 스크립트 종료 (PASS)")

log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")