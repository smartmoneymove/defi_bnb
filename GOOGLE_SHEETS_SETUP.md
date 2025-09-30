# 📊 Настройка Google Sheets для логирования

Google Sheets используется для автоматического логирования данных о работе DeFi Liquidity Manager.

## 🎯 Что логируется?

Система автоматически записывает:
- **Время начала и окончания** рабочих периодов
- **Цена BTCB** на момент начала и окончания
- **Общий баланс портфеля** в USDT
- **Продолжительность работы** в часах
- **Прибыль/убыток** за период
- **Изменение цены BTCB** в процентах

**Мультисерверная поддержка:** Каждый кошелек создает свой отдельный лист автоматически!

## 🚀 Быстрая настройка

### Шаг 1: Создайте Google Cloud Project

1. Перейдите в [Google Cloud Console](https://console.cloud.google.com/)
2. Создайте новый проект или выберите существующий
3. Запомните **Project ID** (понадобится позже)

### Шаг 2: Включите Google Sheets API

1. В Google Cloud Console перейдите в **APIs & Services** → **Library**
2. Найдите **Google Sheets API**
3. Нажмите **Enable** (Включить)
4. Дождитесь активации (1-2 минуты)

Или используйте прямую ссылку:
```
https://console.developers.google.com/apis/api/sheets.googleapis.com/overview?project=YOUR_PROJECT_ID
```

### Шаг 3: Создайте Service Account

1. Перейдите в **APIs & Services** → **Credentials**
2. Нажмите **Create Credentials** → **Service Account**
3. Заполните форму:
   - **Service account name:** `defi-logger` (или любое другое имя)
   - **Service account description:** `DeFi Liquidity Manager Logger`
4. Нажмите **Create and Continue**
5. **Role:** можете оставить пустым (не обязательно для Google Sheets)
6. Нажмите **Continue**, затем **Done**

### Шаг 4: Создайте и скачайте ключ

1. В списке Service Accounts найдите созданный аккаунт
2. Нажмите на него (или на значок ✏️ Edit)
3. Перейдите на вкладку **Keys**
4. Нажмите **Add Key** → **Create new key**
5. Выберите **JSON**
6. Нажмите **Create**
7. Файл JSON будет скачан автоматически

### Шаг 5: Настройте файл service_account.json

```bash
# Переместите скачанный файл в папку проекта
cp ~/Downloads/your-project-xxxxx.json /root/defi_bnb/service_account.json

# Проверьте что файл на месте
ls -la /root/defi_bnb/service_account.json
```

Структура файла (пример):
```json
{
  "type": "service_account",
  "project_id": "your-project-id",
  "private_key_id": "abc123...",
  "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
  "client_email": "defi-logger@your-project-id.iam.gserviceaccount.com",
  "client_id": "123456789012345678901",
  ...
}
```

**ВАЖНО:** Запомните значение поля `client_email` - оно понадобится на следующем шаге!

### Шаг 6: Создайте Google Sheets таблицу

1. Перейдите на [Google Sheets](https://sheets.google.com/)
2. Создайте новую таблицу
3. Назовите её, например: `DeFi BNB Chain Analytics`
4. Скопируйте **ID таблицы** из URL:
   ```
   https://docs.google.com/spreadsheets/d/SPREADSHEET_ID_HERE/edit#gid=0
                                            ^^^^^^^^^^^^^^^^^^^^
                                            Это ваш Spreadsheet ID
   ```

### Шаг 7: Предоставьте доступ Service Account

1. В Google Sheets нажмите **Share** (Поделиться) в правом верхнем углу
2. В поле "Add people and groups" введите **client_email** из `service_account.json`
   - Например: `defi-logger@your-project-id.iam.gserviceaccount.com`
3. Выберите роль **Editor** (Редактор)
4. **ВАЖНО:** Снимите галочку "Notify people" (не нужно отправлять уведомление)
5. Нажмите **Share** (Отправить)

### Шаг 8: Настройте .env

Добавьте в файл `.env`:

```bash
# Google Sheets Configuration
GOOGLE_SHEETS_ID="your_spreadsheet_id_here"
GOOGLE_SERVICE_ACCOUNT_PATH="service_account.json"
```

### Шаг 9: Проверьте настройку

Запустите тестовый скрипт:

```bash
cd /root/defi_bnb
python src/google_sheets_logger.py
```

Если всё настроено правильно, вы увидите:
```
✅ Успешно подключено к Google Sheets
✅ Лист для кошелька 0xYourAddress создан/найден
✅ Тестовая запись добавлена
```

Проверьте вашу Google Sheets таблицу - должен появиться новый лист с названием вашего адреса кошелька.

## 📋 Структура таблицы

Каждый лист содержит следующие колонки:

| Колонка | Описание |
|---------|----------|
| **Start Time** | Время начала периода (UTC) |
| **End Time** | Время окончания периода (UTC) |
| **Duration (hours)** | Продолжительность работы в часах |
| **Start BTCB Price** | Цена BTCB на начало периода |
| **End BTCB Price** | Цена BTCB на конец периода |
| **BTCB Price Change %** | Изменение цены BTCB в процентах |
| **Start Balance (USDT)** | Баланс портфеля на начало |
| **End Balance (USDT)** | Баланс портфеля на конец |
| **Profit/Loss (USDT)** | Прибыль или убыток за период |
| **Profit/Loss %** | Прибыль/убыток в процентах |

## 🔧 Устранение неполадок

### Проблема: "Permission denied" или "403 Forbidden"

**Решение:**
1. Убедитесь что Google Sheets API включен в вашем проекте
2. Проверьте что вы предоставили доступ правильному email из service_account.json
3. Убедитесь что дали роль **Editor**, а не **Viewer**

### Проблема: "File not found: service_account.json"

**Решение:**
```bash
# Проверьте путь к файлу
ls -la /root/defi_bnb/service_account.json

# Если файла нет, скопируйте его снова
cp ~/Downloads/your-project-xxxxx.json /root/defi_bnb/service_account.json

# Проверьте путь в .env
grep GOOGLE_SERVICE_ACCOUNT_PATH .env
```

### Проблема: "Invalid JSON in service_account.json"

**Решение:**
```bash
# Проверьте корректность JSON
cat service_account.json | python -m json.tool

# Если ошибка, скачайте файл заново из Google Cloud Console
```

### Проблема: "Spreadsheet not found"

**Решение:**
1. Проверьте правильность Spreadsheet ID в .env
2. Убедитесь что таблица существует и не удалена
3. Проверьте что service account имеет доступ к таблице

### Проблема: Система работает, но логи не записываются

**Решение:**
```bash
# Проверьте что переменные окружения загружены
python -c "
import os
from dotenv import load_dotenv
load_dotenv()
print('GOOGLE_SHEETS_ID:', os.getenv('GOOGLE_SHEETS_ID'))
print('Service Account Path:', os.getenv('GOOGLE_SERVICE_ACCOUNT_PATH'))
"

# Проверьте логи на наличие ошибок
grep -i "google\|sheets" /path/to/your/logs.log
```

## 🎨 Расширенная настройка

### Форматирование таблицы

После первого запуска вы можете настроить внешний вид:
1. Автоширина колонок
2. Цветовое кодирование (зеленый для прибыли, красный для убытка)
3. Условное форматирование для важных метрик
4. Графики для визуализации данных

### Множественные кошельки

Система автоматически создает отдельный лист для каждого кошелька:
- Название листа = адрес кошелька
- Каждый кошелек имеет свою независимую историю
- Можно запускать несколько инстансов скрипта с разными кошельками

### Экспорт данных

Вы можете экспортировать данные для анализа:
1. **File** → **Download** → **CSV** / **Excel**
2. Используйте Google Sheets API для программного доступа
3. Подключите Google Data Studio для визуализации

## 📚 Дополнительные ресурсы

- [Google Sheets API Documentation](https://developers.google.com/sheets/api)
- [Service Accounts Guide](https://cloud.google.com/iam/docs/service-accounts)
- [Python gspread Library](https://docs.gspread.org/)

## ⚠️ Важные замечания

1. **Безопасность:**
   - НИКОГДА не публикуйте `service_account.json` в git
   - Файл содержит приватный ключ для доступа к вашей таблице
   - Файл уже добавлен в `.gitignore`

2. **Лимиты API:**
   - Google Sheets API имеет лимиты: 100 запросов в 100 секунд на пользователя
   - Скрипт оптимизирован и делает минимум запросов
   - При превышении лимитов происходит автоматическая повторная попытка

3. **Резервное копирование:**
   - Регулярно делайте копии таблицы (**File** → **Make a copy**)
   - Google Sheets имеет историю версий (File → Version history)

4. **Опциональность:**
   - Google Sheets полностью опционален
   - Система работает и без него
   - Все данные также логируются в консоль и файлы состояния

---

**✅ Готово!** Теперь все операции будут автоматически записываться в вашу Google Sheets таблицу!
