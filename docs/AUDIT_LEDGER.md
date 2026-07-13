
Дополнено: single-table SELECT WHERE-колонки (без JOIN — однозначные) просканированы — 0 багов. Остаётся только multi-table/JOIN SELECT (высокий false-rate, отдельный проход при желании). Класс «несуществующая колонка» по однозначным поверхностям (INSERT/UPDATE/single-SELECT) закрыт: 2 фикса (crm_deals, broadcasts.silent) + 1 задокументирован (ranking_engine), остальное чисто.
