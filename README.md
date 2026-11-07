python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in your keys

python -c "import ssl; import urllib.request; from pyngrok import installer, conf; ctx = ssl._create_unverified_context(); installer.install_ngrok(conf.get_default().ngrok_path, ngrok_version='3', context=ctx)"

outside the venv
--------------------
PS C:\D\Anish\FrontDesk> & "C:\Users\anish\AppData\Local\Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe" http 7860

ngrok config add-authtoken 3LWl9Nxa6ZilwrJ4XcN47pMlI2j_3hszhjpYyYK9zWBYE6pzH


ngrok http 7860             # or whatever port the runner prints on startup
python bot.py -t twilio -x YOUR-NAME.ngrok.io
python bot.py -t twilio -x unworldly-corned-action.ngrok-free.dev

TEST
python test\stt_only_bot.py -t twilio -x unworldly-corned-action.ngrok-free.dev

UNIT TEST
python -m unittest discover -s test/unit_tests

Importing menu.
(venv) PS C:\D\Anish\FrontDesk> python admin\import_menu.py scan menu\Bay-Leaf-Menu-1.jpg --dry-run