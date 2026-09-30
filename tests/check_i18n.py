"""CI-проверка: в русском и казахском словарях одинаковые ключи и одинаковые {параметры}."""
import re
import sys

sys.path.insert(0, '.')
from i18n import RU, KK

errors = []
for key in sorted(set(RU) ^ set(KK)):
    errors.append(f"ключ есть не в обоих языках: {key}")
for key in sorted(set(RU) & set(KK)):
    if set(re.findall(r'\{(\w+)\}', RU[key])) != set(re.findall(r'\{(\w+)\}', KK[key])):
        errors.append(f"разные {{параметры}} в переводах: {key}")

if errors:
    print("\n".join(errors))
    sys.exit(1)
print(f"OK: {len(RU)} ключей, переводы согласованы")
