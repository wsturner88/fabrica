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

## Put the quantity in the name (plate contents)
`file_facts` carries `plate_contents`, and `plate_contents(printer, filename)` (printer name or "library") answers it alone: how many
of which object are on the plate, read from the slicer's cancel-object labels. It reads the cache; no printer is contacted.
- `labels: "present"`: `summary` ("4× Hose Clip + 2× Hook"), `total` and `groups` [{name, count}] are exact. If the naming rule wants a
  quantity, write it into the name, for example `4x Hose Clip + 2x Hook`. One object built from several parts counts as ONE.
- `labels: "missing"`: the file was sliced without "Label objects", so the quantity cannot be known (nor can one object be cancelled).
  Do NOT guess a quantity. Keep the name without one and tell the human the file needs re-slicing with labels turned on.
- `labels: "unknown"`: not read yet, only part of the file was read, or the format hides it. Do NOT guess a quantity either.
- `parts` with `parts_source: "labels"` is the same count; `slice_info` is the Bambu file's own count.

## What the Hub does on apply
For each file, on idle printers only: download -> keep a copy in the library -> upload under the new name ->
verify -> delete the old file. A printer that is busy or offline is skipped and reported; nothing is changed there.

## Rules
- Never set `approved_by_human=True` without the human's yes in this conversation.
- The ticket is single use and tied to the preview; if anything changes, make a new preview and ask again.
- Check the result of `rename_apply` and tell the human what was renamed, what was skipped and why.
