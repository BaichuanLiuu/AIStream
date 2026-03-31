#!/usr/bin/env bash
set -euo pipefail

WATCH_DIR="$(cd "$(dirname "$0")" && pwd)/声音"

# 已播放/在队列中的文件（用绝对路径）
declare -A SEEN

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

is_wav() {
  local f="$1"
  [[ "${f,,}" == *.wav ]]
}

# 等待文件写入完成：连续两次stat大小一致才认为完成
wait_file_stable() {
  local f="$1"
  local last_size=""
  local stable_count=0

  while true; do
    if [[ ! -f "$f" ]]; then
      return 1
    fi

    # macOS: stat -f%z
    local size
    size=$(stat -f%z "$f" 2>/dev/null || echo "")

    if [[ -n "$last_size" && "$size" == "$last_size" ]]; then
      stable_count=$((stable_count+1))
    else
      stable_count=0
    fi

    last_size="$size"

    if [[ $stable_count -ge 2 ]]; then
      return 0
    fi

    sleep 0.3
  done
}

# 扫描目录，返回按修改时间排序的wav列表
list_wavs_sorted() {
  # 只列出当前目录下的wav（避免递归）
  # %m: mtime epoch
  find "$WATCH_DIR" -maxdepth 1 -type f \( -iname '*.wav' \) -print0 \
    | xargs -0 stat -f '%m %N' 2>/dev/null \
    | sort -n \
    | cut -d' ' -f2-
}

play_one() {
  local f="$1"
  log "播放：$(basename "$f")"
  # afplay 阻塞直到播放结束
  afplay "$f" || true
  log "播放结束：$(basename "$f")"
}

main() {
  if [[ ! -d "$WATCH_DIR" ]]; then
    log "目录不存在：$WATCH_DIR"
    exit 1
  fi

  log "开始监听目录：$WATCH_DIR"
  log "提示：把新的 TTS wav 放到该目录，会自动排队播放。"

  while true; do
    local any_found=0

    while IFS= read -r f; do
      any_found=1
      # 绝对路径
      local abs="$f"
      if [[ -z "${SEEN["$abs"]+x}" ]]; then
        # 标记为seen，避免重复入队
        SEEN["$abs"]=1

        # 等文件写完
        log "检测到新文件：$(basename "$abs")，等待写入完成..."
        if wait_file_stable "$abs"; then
          play_one "$abs"
        else
          log "文件消失/不可读，跳过：$(basename "$abs")"
        fi
      fi
    done < <(list_wavs_sorted || true)

    # 如果目录暂时没有文件，或没发现新文件，稍等
    sleep 0.5
  done
}

main "$@"

