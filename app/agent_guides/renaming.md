# Renaming files on printers

The Hub provides the MECHANICS only. How files should be named is the owner's or your own rule, never the Hub's.

## Flow
1. `file_facts` shows what is known about the files (name, size, printer, plate, filaments, sliced-for model).
   Read it; it is the raw material for your names.
2. Decide the new names with your own rules (or the owner's). Do not invent a naming scheme the human did not ask for.
3. `rename_preview` takes your proposed names and returns a before -> after table and a single-use ticket.
   Nothing has changed yet.
4. Show the table to the human, unchanged. Ask: "Apply these renames?"
5. Only after an explicit yes: `rename_apply(ticket, approved_by_human=True)`.

## What the Hub does on apply
For each file, on idle printers only: download -> keep a copy in the library -> upload under the new name ->
verify -> delete the old file. A printer that is busy or offline is skipped and reported; nothing is changed there.

## Rules
- Never set `approved_by_human=True` without the human's yes in this conversation.
- The ticket is single use and tied to the preview; if anything changes, make a new preview and ask again.
- Check the result of `rename_apply` and tell the human what was renamed, what was skipped and why.
