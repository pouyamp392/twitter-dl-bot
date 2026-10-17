            "⌛ این درخواست منقضی شده؛ لینک توییت را دوباره بفرست."
        )
        return

    if query.from_user.id != entry["user_id"]:
        await query.answer("این دکمه برای تو نیست.", show_alert=True)
        return

    if entry["busy"]:
        await query.answer("در حال انجام است...")
        return

    entry["busy"] = True
    await query.answer()
    await query.edit_message_text(f"⏳ در حال دانلود ({option['height']}p)...")

    chat_id = query.message.chat_id

    try:
        async with DOWNLOAD_SEMAPHORE:
            with TemporaryDirectory() as tmpdir:
                opts = base_opts(tmpdir)
                opts["format"] = fid

                def download():
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        return ydl.extract_info(entry["url"], download=True)

                info = await asyncio.to_thread(download)

                entries = info.get("entries")
                if entries and any(entries):
                    info = [e for e in entries if e][0]

                video_exts = {".mp4", ".mkv", ".webm", ".mov"}
                files = [
                    p for p in Path(tmpdir).iterdir()
                    if p.is_file()
                    and p.suffix.lower() in video_exts
                    and p.stat().st_size > 0
                ]

                if not files:
                    raise FileNotFoundError("Downloaded video was not found")

                filename = max(files, key=lambda p: p.stat().st_size)

                if filename.stat().st_size > MAX_FILE_SIZE:
                    await query.edit_message_text(
                        "❌ حجم این ویدئو از سقف ارسال ربات (حدود ۵۰ مگابایت) بیشتر است."
                    )
                    entry["busy"] = False
                    return

                await query.edit_message_text("📤 در حال ارسال...")

                with filename.open("rb") as video:
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=video,
                        supports_streaming=True,
                        duration=int(info.get("duration") or entry.get("duration") or 0) or None,
                        width=option.get("width"),
                        height=option.get("height"),
                        caption="🎬",
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                        pool_timeout=30,
                    )

        await query.message.delete()
        PENDING.pop(token, None)

    except Exception as e:
        logger.exception("Tweet video download failed")

        # جایگزین: لینک مستقیم FxTwitter
        fx_video = entry.get("fx_video")
        if fx_video:
            try:
                await query.edit_message_text("⏳ در حال تلاش با روش دوم...")
                if await send_video_from_url(context, chat_id, fx_video):
                    await query.message.delete()
                    PENDING.pop(token, None)
                    return
            except Exception:
                logger.exception("Fallback video send failed")

        entry["busy"] = False
        await query.edit_message_text(f"❌ خطا:\n{str(e)[:700]}")


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    # خطاهای شبکه‌ی گذرا (مثل Conflict هنگام دیپلوی) را فقط کوتاه لاگ می‌کنیم
    logger.warning("Bot error: %s", context.error)


# ---------------- Main ----------------

def main():
    threading.Thread(
        target=start_health_server,
        daemon=True,
    ).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_join_check, pattern=r"^join:check$"))
    app.add_handler(CallbackQueryHandler(on_quality, pattern=r"^q:"))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_url,
        )
    )
    app.add_error_handler(on_error)

    logger.info("Twitter bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
