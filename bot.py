import os
import asyncio
import json
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import pandas as pd
import requests

# ─── CONFIG ───────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
ALIBABA_API_KEY = os.environ.get("ALIBABA_API_KEY", "").strip()
ALIBABA_BASE_URL = "https://coding-intl.dashscope.aliyuncs.com/apps/anthropic"
MEMORY_DIR = "memory"
UPLOADS_DIR = "uploads"

print(f"TOKEN LENGTH: {len(TELEGRAM_TOKEN)}")
print(f"TOKEN STARTS: {TELEGRAM_TOKEN[:10] if TELEGRAM_TOKEN else 'EMPTY'}")

os.makedirs(MEMORY_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)


# ─── STEP 1: READ EXCEL & BUILD MEMORY ───────────────────
def build_memory(df: pd.DataFrame, file_type: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if file_type == "invoices":
        _build_invoices_memory(df, now)
    elif file_type == "shipping_reply":
        _build_shipping_reply_memory(df, now)


def _build_invoices_memory(df: pd.DataFrame, now: str):
    total = len(df)
    total_revenue = df["إجمالي الفاتورة (جنيه)"].sum()
    date_range = f"{df['تاريخ الفاتورة'].min()} إلى {df['تاريخ الفاتورة'].max()}"

    summary = f"""# ملخص الأسبوع
آخر تحديث: {now}

## الأرقام الأساسية
- إجمالي الأوردرات: {total}
- إجمالي الإيراد: {total_revenue:,.0f} جنيه
- الفترة: {date_range}
"""
    _save(summary, "ملخص_الاسبوع.md")

    regions = df.groupby("المحافظة").agg(
        عدد_الأوردرات=("رقم الفاتورة", "count"),
        إجمالي_الإيراد=("إجمالي الفاتورة (جنيه)", "sum")
    ).sort_values("عدد_الأوردرات", ascending=False)

    regions_md = f"# تحليل المناطق\nآخر تحديث: {now}\n\n"
    for region, row in regions.iterrows():
        regions_md += f"## {region}\n- عدد الأوردرات: {row['عدد_الأوردرات']}\n- الإيراد: {row['إجمالي_الإيراد']:,.0f} جنيه\n\n"
    _save(regions_md, "المناطق.md")

    products = df.groupby("المنتج").agg(
        عدد_العبوات=("الكمية", "sum"),
        إجمالي_الإيراد=("إجمالي الفاتورة (جنيه)", "sum")
    ).sort_values("عدد_العبوات", ascending=False)

    products_md = f"# تحليل المنتجات\nآخر تحديث: {now}\n\n"
    for product, row in products.iterrows():
        products_md += f"## {product}\n- عدد العبوات: {row['عدد_العبوات']}\n- الإيراد: {row['إجمالي_الإيراد']:,.0f} جنيه\n\n"
    _save(products_md, "المنتجات.md")

    daily = df.groupby("تاريخ الفاتورة").agg(
        عدد_الأوردرات=("رقم الفاتورة", "count"),
        إجمالي_الإيراد=("إجمالي الفاتورة (جنيه)", "sum")
    )
    daily_md = f"# التحليل اليومي\nآخر تحديث: {now}\n\n"
    for date, row in daily.iterrows():
        daily_md += f"## {date}\n- أوردرات: {row['عدد_الأوردرات']}\n- إيراد: {row['إجمالي_الإيراد']:,.0f} جنيه\n\n"
    _save(daily_md, "التحليل_اليومي.md")


def _build_shipping_reply_memory(df: pd.DataFrame, now: str):
    status_col = "حالة التوصيل"
    total = len(df)
    delivered = len(df[df[status_col] == "مسلّم"])
    returned = len(df[df[status_col] == "مرتجع"])
    in_transit = len(df[df[status_col] == "قيد التوصيل"])
    failed = len(df[df[status_col] == "فشل التوصيل"])

    returns_md = f"""# تقرير التوصيل والمرتجعات
آخر تحديث: {now}

## ملخص التوصيل
- إجمالي: {total}
- مسلّم: {delivered} ({delivered/total*100:.1f}%)
- مرتجع: {returned} ({returned/total*100:.1f}%)
- قيد التوصيل: {in_transit} ({in_transit/total*100:.1f}%)
- فشل التوصيل: {failed} ({failed/total*100:.1f}%)
"""
    returned_df = df[df[status_col] == "مرتجع"]
    if not returned_df.empty:
        reasons = returned_df["سبب المرتجع"].value_counts()
        returns_md += "\n### أسباب المرتجعات\n"
        for reason, count in reasons.items():
            returns_md += f"- {reason}: {count} حالة\n"
        returns_md += "\n### المرتجعات حسب المحافظة\n"
        region_returns = returned_df["المحافظة"].value_counts()
        for region, count in region_returns.items():
            returns_md += f"- {region}: {count} مرتجع\n"

    _save(returns_md, "التوصيل_والمرتجعات.md")


def _save(content: str, filename: str):
    path = os.path.join(MEMORY_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# ─── STEP 2: READ MEMORY & TALK TO AI ────────────────────
def read_memory() -> str:
    all_memory = ""
    for filename in os.listdir(MEMORY_DIR):
        if filename.endswith(".md"):
            path = os.path.join(MEMORY_DIR, filename)
            with open(path, "r", encoding="utf-8") as f:
                all_memory += f"\n\n---\n\n{f.read()}"
    return all_memory


def ask_ai(question: str, memory: str) -> str:
    system_prompt = """أنت مستشار أعمال ذكي لشركة مبيدات حشرية مصرية.
عندك كل بيانات الشركة في ذاكرتك.
بتتكلم بالعربي بشكل طبيعي وبسيط.
بتحلل وبتجاوب وبتقترح وبتلاحظ الأنماط الغريبة.
لو مش لاقي معلومة — قول بصراحة."""

    user_message = f"""## بيانات الشركة:
{memory}

## سؤال المستخدم:
{question}"""

    response = requests.post(
        f"{ALIBABA_BASE_URL}/v1/messages",
        headers={
            "x-api-key": ALIBABA_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json={
            "model": "qwen3-max-2026-01-23",
            "max_tokens": 1000,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}]
        },
        timeout=30
    )
    data = response.json()
    return data["content"][0]["text"]


# ─── STEP 3: DETECT FILE TYPE ─────────────────────────────
def detect_file_type(df: pd.DataFrame) -> str:
    columns = list(df.columns)
    if "حالة التوصيل" in columns:
        return "shipping_reply"
    elif "تاريخ الفاتورة" in columns:
        return "invoices"
    return "unknown"


# ─── STEP 4: TELEGRAM HANDLERS ───────────────────────────
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = update.message.document
    if not file.file_name.endswith((".xlsx", ".xls")):
        await update.message.reply_text("❌ بعتلي ملف Excel بس (.xlsx)")
        return

    await update.message.reply_text("⏳ جاري قراءة الملف...")

    tg_file = await context.bot.get_file(file.file_id)
    file_path = os.path.join(UPLOADS_DIR, file.file_name)
    await tg_file.download_to_drive(file_path)

    df = pd.read_excel(file_path)
    file_type = detect_file_type(df)

    if file_type == "unknown":
        await update.message.reply_text("❌ مش عارف أتعرف على الملف ده")
        return

    build_memory(df, file_type)

    if file_type == "invoices":
        await update.message.reply_text(
            f"✅ تمام! قرأت {len(df)} فاتورة وحفظتهم في ذاكرتي\n"
            "دلوقتي تقدر تسألني أي حاجة 🎯"
        )
    elif file_type == "shipping_reply":
        delivered = len(df[df["حالة التوصيل"] == "مسلّم"])
        returned = len(df[df["حالة التوصيل"] == "مرتجع"])
        await update.message.reply_text(
            f"✅ استلمت رد شركة الشحن\n"
            f"مسلّم: {delivered} | مرتجع: {returned}\n"
            "سألني أي حاجة 🎯"
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = update.message.text
    memory = read_memory()

    if not memory.strip():
        await update.message.reply_text(
            "📂 مفيش بيانات عندي لسه\n"
            "ابعتلي شيت Excel الأول 🙏"
        )
        return

    await update.message.reply_text("⏳ بفكر...")

    try:
        answer = ask_ai(question, memory)
        await update.message.reply_text(answer)
    except Exception as e:
        await update.message.reply_text(f"❌ حصل خطأ: {str(e)}")


# ─── MAIN ─────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN is empty!")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 البوت شغال...")
    app.run_polling()


if __name__ == "__main__":
    main()
