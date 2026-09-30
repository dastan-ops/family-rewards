#!/usr/bin/env bash
# Запускается НА СЕРВЕРЕ из папки проекта (GitHub Actions делает это сам).
# Можно и вручную: cd <папка проекта> && bash deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "== 1/4 Резервная копия базы =="
mkdir -p backups
docker exec postgres_db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > "backups/backup_$(date +%F_%H%M).sql"
ls -1t backups/*.sql | tail -n +11 | xargs -r rm --   # хранить только 10 последних копий

echo "== 2/4 Забираем свежий код =="
git pull --ff-only origin main   # если на сервере остались ручные правки — остановится с ошибкой, а не затрёт их

echo "== 3/4 Сборка и запуск =="
docker compose up -d --build

echo "== 4/4 Проверка, что сайт отвечает =="
for i in $(seq 1 20); do
  if curl -fsS -o /dev/null http://localhost/; then
    echo "OK: сайт отвечает"
    exit 0
  fi
  sleep 3
done
echo "ОШИБКА: сайт не отвечает. Последние логи:"
docker compose logs --tail=50 web
exit 1
