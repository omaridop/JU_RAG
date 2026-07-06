# Claude prompt — turn a the University of Jordan PDF into a JU_RAG Q&A JSON

Use this in **Claude / Claude Cowork**: attach the PDF, paste the prompt below, and
Claude writes a `.json` file. Upload that file in the admin panel
(**المستندات → رفع ملف**). JSON files are **embedded and stored directly — the
reader model never runs on them**, so the file must already be in this exact shape.

The app accepts the file via the `qa` array (`app/pipeline/qa_loader.py` →
`normalize_qa_units` → `doc_to_qa.qa_units_to_prepared`). Each question paraphrase
becomes its own vector pointing at the shared answer, plus an answer-content vector.

> **About the date & source:** you pick the **effective date** and an optional
> **source label** in the admin panel at upload time — those win over anything in
> the file. The `metadata` block below is optional and just for your own reference.

---

## The prompt (copy everything in this block)

```
You are a senior bilingual (Arabic & English) NLP engineer building a Q&A
knowledge base for University of Jordan students from an official university
document. I am giving you ONE PDF. Read it end to end and produce a single JSON
file in the EXACT schema described below, then save it as a .json file.

# READ WITH STRICT ATTENTION
Read every page, including tables, footnotes, headers/footers, and small print.
Capture EVERY fact a student could ask about: dates, deadlines, fees, numbers,
percentages, names, emails, phone numbers, room/office numbers, requirements,
steps, conditions, exceptions, and table rows.

# GROUNDING (non-negotiable)
- Every answer must be grounded ONLY in this PDF. Copy exact numbers, dates,
  names, fees and amounts verbatim.
- NEVER invent, assume, generalize, or add outside knowledge. If the PDF does
  not state something, do not answer it.
- If the document has no usable information, output {"qa": []}.

# OUTPUT SCHEMA (return STRICT JSON only — no markdown, no comments, no prose)
{
  "metadata": {
    "source": "<short human title of this document>",
    "date": "YYYY-MM-DD"
  },
  "qa": [
    {
      "topic": "<short topic, 2-5 words>",
      "page": <integer page number where the fact appears>,
      "questions_en": ["q1", "q2", "q3", "q4"],
      "answer_en": "<precise, complete English answer grounded in the PDF>",
      "questions_ar": ["س1", "س2", "س3", "س4"],
      "answer_ar": "<the same precise answer in Arabic — a faithful translation>"
    }
  ]
}

# RULES FOR EACH Q&A UNIT
- One unit = one distinct fact. Prefer MANY focused units over a few broad ones.
- "questions_en": at least 4 DISTINCT English paraphrases of the same question.
  Vary wording, structure, synonyms, and register (formal and casual, direct and
  indirect) — students phrase the same question many ways.
- "questions_ar": at least 4 DISTINCT Arabic paraphrases. Mix Modern Standard
  Arabic with the way students actually ask. Vary the phrasing.
- "answer_en" and "answer_ar" must be faithful translations of each other and
  each fully self-contained (don't say "see above" or rely on other units).
- "page": the integer page where the fact is stated. If a fact spans pages, use
  the page where it is primarily stated.
- "topic": a short label, e.g. "Tuition - Engineering", "Withdrawal deadline".

# TABLE COVERAGE (do not skip rows)
- For a table where each row describes an entity (a major, course, fee item,
  office, professor...), create ONE unit PER ROW whose answer includes ALL of
  that row's columns (e.g. for a major: its faculty, minimum admission average,
  plan hours, and every fee). Never skip a row.
- ALSO add ONE comprehensive unit per table or coherent section that answers
  overview questions in a single answer: totals AND their full breakdown,
  complete lists, all steps of a procedure. Students often ask for "all the
  requirements", "the whole plan", or "how is it distributed".

# ARABIC QUALITY
- Use correct, natural Arabic. Keep official terms exact. For University of
  Jordan program names, preserve the document's wording (e.g. «البرنامج العادي /
  التنافسي», «الموازي», «الدولي») — do not swap or merge them.

# OUTPUT FORMAT
- Return ONLY the JSON object. No ```json fences, no explanations before or after.
- Valid JSON, UTF-8, Arabic written as real characters (not \uXXXX escapes).
- Be exhaustive: it is normal for a rich document to yield dozens or hundreds of
  units. Do not summarize or truncate to save space.
- Save the result as a file named: <short-document-name>_qa.json
```

---

## Quick quality checklist (verify before you upload)

- [ ] Top level is a JSON object with a `"qa"` array (or `{"qa": []}` if empty).
- [ ] Every unit has `topic`, integer `page`, `questions_en`, `answer_en`,
      `questions_ar`, `answer_ar`.
- [ ] Each `questions_en` / `questions_ar` has **4+ distinct** paraphrases.
- [ ] `answer_en` and `answer_ar` say the same thing; numbers/dates/fees match the
      PDF exactly.
- [ ] No table rows skipped; each big table also has one comprehensive overview unit.
- [ ] The file is valid UTF-8 JSON with no markdown fences.

## Filled example (one unit — what a good entry looks like)

```json
{
  "metadata": { "source": "University of Jordan — General Park", "date": "2026-06-14" },
  "qa": [
    {
      "topic": "General Park - Admission Fee",
      "page": 1,
      "questions_en": [
        "What is the admission fee for the university's general park?",
        "How much does it cost to enter the public garden?",
        "What is the entry fee for the general park?",
        "How many dinars to get into the university park?"
      ],
      "answer_en": "The admission fee for the University's general park is 5 dinars, paid once every semester.",
      "questions_ar": [
        "كم تبلغ رسوم دخول الحديقة العامة للجامعة؟",
        "ما هي تكلفة الدخول إلى الحديقة العامة؟",
        "كم ديناراً يجب دفعه لدخول الحديقة العامة؟",
        "ما رسوم الدخول للحديقة العامة في الجامعة؟"
      ],
      "answer_ar": "رسوم دخول الحديقة العامة للجامعة 5 دنانير، تُدفع مرة واحدة كل فصل دراسي."
    }
  ]
}
```

> **Tip:** If a PDF is very large, ask Claude to process it in page ranges and then
> concatenate all units into one `"qa"` array before saving. The app de-duplicates
> repeated question paraphrases automatically.
