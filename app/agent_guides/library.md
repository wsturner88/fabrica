# Library

The library is a folder of print files kept on the Hub, plus a record of which printers already hold which file.

- `library_list(query="", folder="")`: files on the Hub and files known to be on printers (from cached listings;
  it never contacts a printer). `query` matches name, folder, printer, "sliced for" model and filament words.
  Each row says `in_library` and `on_printers`.
- `library_file(name)`: one file in detail: print time, weight, layers, temperatures, slicer, the printer model it
  was sliced for, filaments as colour words with grams, and which printers hold it.
- `library_send(name, printers, model_override=False)`: UPLOADS the file to the named printers (idle, printing or paused).
  It never starts a print. Printing is a separate flow: see topic `printing`.
- `library_save_from_printer(printer, filename)`: copies a file from a printer into the library (the printer keeps it).

## Rules
- A printer that is idle, printing or paused can be sent a file. One that is offline, not connected or just had a print started is refused per printer and nothing is sent to it.
- While a printer is printing or paused, a file name it already has is never replaced (refused as `in_use`): that could be the file being printed. Use another name, or wait until it is idle.
- If the human turned off "send files to printers that are printing or paused" (Settings, File library), only idle printers take a file, as before.
- A file already on the printer is not overwritten.
- If the file was sliced for a different printer model it is refused. Only if the human read the warning and says it
  is safe may you pass `model_override=True`.
- Upload only what the human asked for, and say which printers you sent it to.
- `library_list` and `library_file` need a read key; `library_send` and `library_save_from_printer` need a control key.
