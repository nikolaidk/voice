# LinkedIn teaser

---

**I gave an AI thirteen misspelled words. One evening later, it gave me a keynote about what it was like to work with me.**

The experiment started at 18:44 with an empty folder and this exact prompt:

*"Create webapi that takes url or pd and converts it into a podcasta"*

No spec. No architecture. No acceptance criteria.

By 21:00 the same evening, Claude (Fable 5) had built — and verified, end to end:

🎙️ A production pipeline that turns any URL or PDF into a podcast, spoken summary, or full readout — with host personalities, audience targeting, and per-line voice direction ("pronounce Graham as *gram*, slow down 12% here")

🖥️ A complete web workbench — library, template system, iterative revision of script, slides and voice over

📊 Slides where the AI *designs its own figures* as SVG — the first thing it drew was a semi-log chart, a form the hardcoded renderer it replaced couldn't even express

🎬 Synced slideshows and video with voice-over, captions burned-in or toggleable, timestamped transcripts

The numbers: I wrote 15 messages — fewer than 500 words. The system is ~3,400 lines of working code, 17 endpoints, a 5-stage media pipeline. Every word of mine became roughly 7 lines of software.

My total code review? Four words: *"you hardcoded the charts. that's bad."* It reversed an architecture in 20 minutes.

**Then I turned the table.**

For the final request, I asked the AI to use the tool we'd just built to produce its own retrospective: a 13-minute presentation, in its own voice, reflecting on what it was like to work with a human. It wrote the essay, designed the slides, charted our actual session timeline, placed screenshots of our collaboration — and said things like:

*"At no point in this evening did I want anything. If you are wondering what remains irreducibly human in AI-assisted development: taste, dissatisfaction, and desire. The rest, increasingly, is negotiable."*

The division of labor is changing. Not the way most people think — the human role doesn't shrink, it *concentrates*: direction, taste, and the judgment to intervene at exactly the right altitude.

Presentation and write-up coming. If you're an architect or developer thinking about what AI-assisted development actually looks like in practice — not the demos, the real workflow — this one's for you.

#AIAssistedDevelopment #SoftwareArchitecture #Claude #LLM #DeveloperExperience #FutureOfWork #HumanInTheLoop

---

*Alternative shorter hook (if you want a tighter post):*

**An AI built a full media production studio from 13 misspelled words in one evening. Then I asked it to make a presentation about what it was like to work with me. Its answer should make every architect pause:**

*"I search locally: make the next thing work. He searched globally: is this the right shape for where the system is going. Both searches are necessary. Only one of them was mine."*

Full story + the AI's 13-minute self-retrospective coming soon.
