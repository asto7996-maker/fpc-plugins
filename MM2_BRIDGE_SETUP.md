# MM2 Automation Bridge

`Connection refused` в `/mm2 -> Roblox Auth -> Bridge` означает, что bridge не запущен по адресу из `automation_bridge.base_url`.

## Где запускать

Bridge запускается на машине, где открыт Roblox-клиент аккаунта-бота.

- Если Cardinal и Roblox на одной машине: `base_url = http://127.0.0.1:8765`
- Если Cardinal на Linux VPS, а Roblox на Windows-ПК: `base_url = http://IP_ПК:8765`

`127.0.0.1` всегда означает "эта же машина", поэтому с VPS он не увидит bridge на вашем домашнем ПК.

## Установка на Windows

Скачайте `tools/mm2_automation_bridge.py` и запустите:

```powershell
py -m pip install pyautogui pillow pyperclip
py mm2_automation_bridge.py --host 0.0.0.0 --port 8765
```

Проверка на этой же машине:

```powershell
curl http://127.0.0.1:8765/health
```

Должно вернуться:

```json
{
  "ok": true,
  "status": "SUCCESS"
}
```

## Настройка Cardinal

В Telegram:

```text
/mm2 -> Настройки -> Bridge URL
```

Если bridge на том же сервере:

```text
http://127.0.0.1:8765
```

Если bridge на Windows-ПК:

```text
http://192.168.1.10:8765
```

После этого:

```text
/mm2 -> Roblox Auth -> Проверить Bridge
```

## Важно про трейд

Bridge открывает Roblox и передаёт Cardinal-команды. Чтобы реально нажимать кнопки MM2 trade, нужно настроить `trade.macro` в `mm2_bridge_config.json` под ваш экран/интерфейс:

- `click` — клик по координатам;
- `paste` — вставить текст, например `{roblox_username}` или `{item_name}`;
- `image_click` — найти картинку на экране и кликнуть;
- `press`, `hotkey`, `sleep`.

Без настроенного `trade.macro` bridge честно ответит:

```text
trade.enabled=false
```

Это означает: сервер открывается, но действия трейда ещё не настроены.
