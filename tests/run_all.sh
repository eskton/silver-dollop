#!/bin/sh
# Прогон всех тестов (поддельный Playerok/Fragment/кошелёк, без сети).
cd "$(dirname "$0")/.." || exit 1
fail=0
for f in tests/*.py; do
  out=$(python "$f" 2>&1 | tail -1)
  case "$out" in
    *OK*) echo "ok    $f" ;;
    *) echo "FAIL  $f: $out"; fail=1 ;;
  esac
done
exit $fail
