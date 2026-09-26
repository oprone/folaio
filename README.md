<p align="center"><img src="web/brand/logo.svg" width="110" alt="Folaio logo"></p>
<h1 align="center">Folaio</h1>
<p align="center"><b>An AI that learns from your PDFs. Its own AI, 100% offline, light enough for any computer.</b></p>

> ⚠️ Early development (v0.2). Installers for Mac and Windows are coming. For now, run it from source (below).

## Why Folaio?

- 🧠 **Its own AI**: Folaio Core is a neural network written from scratch. It learns *your* documents on *your* computer. No pretrained model, nothing to download.
- 🔒 **Private**: your documents never leave your computer. No account, no internet.
- 💻 **Light**: learns about ten textbooks in seconds and runs on ordinary laptops. No GPU needed.
- 📄 **Trustworthy**: answers are the documents' own sentences, each with its page number. If the answer isn't in your documents, Folaio says so.
- 🎓 **Learn faster**: summaries and fill-in-the-blank quizzes with spaced repetition. Quiz answers are taken from your text, so they're never wrong.
- 🗺️ **Knowledge Map**: every chapter coloured by how well you know it (mastered, shaky, weak, not studied), with "Focus next" and one-click practice.
- 📅 **Daily plan**: review what's due, strengthen weak chapters and learn new ones within the time you have, paced for your exam date, with a study streak.
- 📝 **Past-paper analysis**: add past exam papers; Folaio splits them into questions, links each one to the chapter that answers it, and shows which topics are examined most. Your daily plan puts those chapters first.
- 📦 **Subject Packs**: share a whole subject (books, past papers, summaries and quiz questions) as one `.folaio` file by USB, email or WhatsApp. It opens offline, ready to study, and never includes anyone's personal progress.
- ✍️ **Answer checker**: write an answer in your own words; Folaio shows which of the book's key points you covered or missed, and flags claims your book doesn't support.

## How Folaio Core works

```
PDF ─► Reader ─► passages (page-numbered) ─► FolaioNet learns your library
                                                    │
        your question ─► keyword score (BM25) ──────┤
                         + learned meaning ─────────┤
                         + learned topic ───────────┘
                                   │
                                   ▼
             best sentences from your documents, with page citations
```

1. **Reader** turns each PDF into passages, keeping headings and page numbers.
2. **FolaioNet**, a one-hidden-layer neural network in plain NumPy, trains on your library. It learns which section every sentence belongs to. The chapters and sections are the labels, so nobody has to tag anything. While learning that, its hidden layer learns which words and ideas belong together.
3. **Search** combines keyword matching (BM25) with what FolaioNet learned.
4. **Answers** are the best-matching sentences from your documents. **Summaries** are the sentences that best represent each section. **Quizzes** blank out a key term, and FolaioNet picks related terms as the wrong choices.
5. If most of a question's words never appear in your library, Folaio says *"I couldn't find this in your documents"* instead of guessing.

The approach was inspired by [Cony AI](https://github.com/TechieCony/Text-Classifier), extended to learn from PDFs without hand-made labels and to use sparse math, so it scales to whole books.

**Measured**: on about 6 MB of text (roughly ten textbooks), Folaio Core learned in ~6 s. It recognised which document an unseen sentence came from 83% of the time, and searches take a few milliseconds.

## ✨ Folaio Plus (optional)

Folaio Plus explains answers in simple words and writes extra summaries. It uses a very small open AI model, downloaded once (from the welcome screen or **Settings**) and run offline through the embedded llama.cpp engine. Folaio works fully without it.

| Plus brain | Model | Download |
|---|---|---|
| Mini | SmolLM2 360M Instruct (Apache-2.0) | 0.27 GB |
| Small | Qwen2.5 0.5B Instruct (Apache-2.0) | 0.40 GB |

Only very small brains are offered, so Folaio stays light. Because tiny models can make mistakes, every answer shows Folaio Core's exact sentences from your documents first, and the Plus brain adds a short "In simple words" explanation underneath.

## Run from source

Requires Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

Folaio opens in your browser at `http://127.0.0.1:8765`. It only listens on your own computer.

### Where your data lives

Everything stays inside the Folaio folder, in `data/`:

```
Folaio/
└── data/
    ├── brains/        Plus brains (you can choose another folder on the 🧠 Brains page)
    ├── library/       your PDFs, the database, and inbox/
    ├── mind/          what Folaio Core has learned
    └── settings.json
```

So the whole app, with your library, can be copied to another computer or carried on a USB drive. Any PDF or `.folaio` pack copied into `data/library/inbox` is added automatically. (If the Folaio folder can't be written to, for example an installed app, Folaio uses the computer's usual app-data folder instead.)

## Roadmap

- [x] Folaio Core: own neural network trained on your PDFs
- [x] Answers with page citations, summaries, quizzes with spaced repetition
- [x] Optional Folaio Plus (tiny offline models)
- [x] Knowledge Map, daily study plan with exam countdown, answer checker, past-paper analysis, Subject Packs
- [ ] One-click installers for Mac and Windows
- [ ] OCR for scanned PDFs
- [ ] Follow-up questions (chat memory)
- [ ] Glossary of key terms

## Credits

[pypdfium2](https://github.com/pypdfium2-team/pypdfium2) (Apache-2.0/BSD), [NumPy](https://numpy.org) (BSD), [SQLite](https://sqlite.org) (public domain), [FastAPI](https://fastapi.tiangolo.com) (MIT). For Folaio Plus: [llama.cpp](https://github.com/ggml-org/llama.cpp) and [llama-cpp-python](https://github.com/abetlen/llama-cpp-python) (MIT), [SmolLM2](https://huggingface.co/HuggingFaceTB/SmolLM2-360M-Instruct) and [Qwen2.5](https://github.com/QwenLM) models (Apache-2.0). Inspired by [Cony AI](https://github.com/TechieCony/Text-Classifier).

## License

MIT. See [LICENSE](LICENSE).
