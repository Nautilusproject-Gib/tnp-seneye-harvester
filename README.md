# Historical Seneye exports

Put the CSV files Seneye send here, then run the **Import Seneye history**
workflow from the Actions tab. Nothing needs installing: upload the files in
the browser with *Add file → Upload files* on the repo's Code tab.

Run it once with **dry run** ticked and read the log before running it for
real. The dry run writes nothing and tells you which columns it matched, how
many rows it could read, the period they span and the reason for anything it
would skip.

Re-running an import is safe. Readings are keyed on the device and the reading
time, so a file that has already been loaded adds nothing the second time, and
an imported file can never overwrite a reading the harvester collected.

Once a file has been imported you can delete it from this folder — the readings
live in the database from then on. Keeping it does no harm either, beyond the
repository size.
