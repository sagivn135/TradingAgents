# סביבת העבודה המשותפת

המאגר האישי: https://github.com/sagivn135/TradingAgents
מקור upstream: https://github.com/TauricResearch/TradingAgents

## חלוקת עבודה

- Cursor ו־Codex: לפתוח את אותה תיקיית פרויקט, לקרוא AGENTS.md ולשמור שינויים ב־Git. בעבודה בו־זמנית להשתמש בענפים וב־worktrees נפרדים.
- ChatGPT: אפיון, מחקר וסקירה. חיבור החשבון שלו ל־GitHub נפרד מהחיבור של Codex; מסמך זה אינו מפעיל אותו. אפשר למסור לו את docs/CHATGPT_CONTEXT.md. החלטות מוסכמות חוזרות למסמכי המאגר.
- LM Studio: שרת מודל מקומי שהיישום פונה אליו דרך OpenAI-compatible API. הוא אינו עורך את המאגר ואינו מסתנכרן עם היסטוריית הצ׳אטים.

## הפעלה

```sh
uv venv --python 3.12
uv pip install --python .venv/bin/python -e '.[dev]'
cp .env.lmstudio.example .env
lms server start --port 1234 --bind 127.0.0.1
```

יש להוריד ולטעון ב־LM Studio מודל שיחה שתומך בקריאות כלים ובהקשר מספיק גדול. מודל embedding לבדו אינו מספיק. להחליף את SET_LOADED_MODEL_ID בשני השדות בקובץ .env במזהה המדויק שמופיע בשרת.

```sh
.venv/bin/python scripts/check_local_model.py
.venv/bin/python -m cli.main
```

הבדיקה הראשונה בודקת זמינות של המודל בלבד; אינה מאמתת איכות המלצות או תמיכה בכלים. הרצה מלאה מחייבת בדיקה נוספת. אין שימוש בחשבון ChatGPT כמפתח API; מעבר לספק ענן מצריך הרשאות וחיוב API נפרדים.

## הרחבת מידע

מקורות חדשים ישולבו תחת tradingagents/dataflows לפי מנגנון הניתוב הקיים. לכל מקור נגדיר schema, מקור, תאריך פרסום, מגבלות זמינות ובדיקות. מידע פרטי נשמר ב־data/private/ המוחרג מ־Git. דוחות ב־reports/ ומטמונים ב־.local/ מוחרגים גם הם.

## בדיקות ועדכונים

```sh
.venv/bin/ruff check .
.venv/bin/pytest -q
 git fetch upstream
```

לפני שילוב עדכוני upstream יש לבדוק diff ולרוץ על הבדיקות. אין להחליף שינויים מקומיים באמצעות reset --hard.
