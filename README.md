# Foundry Agent Avatar

A browser voice and avatar client for an existing Microsoft Foundry agent. The FastAPI backend authenticates with Microsoft Entra ID and bridges browser audio and WebRTC signaling to Azure Voice Live.

## Configure

1. Confirm the agent works with Voice mode in the Foundry Agent playground.
2. Assign your local identity the **Foundry User** role on the Foundry resource.
3. Copy `env.example` to `.env` and set `VOICELIVE_ENDPOINT`, `AGENT_NAME`, and `PROJECT_NAME`.
4. Ensure the resource region supports avatars: Southeast Asia, North Europe, West Europe, Sweden Central, South Central US, East US 2, or West US 2.

For cross-resource setups, assign the Voice Live resource managed identity the **Foundry User** role on the agent resource and set the two cross-resource variables in `.env`.

## Run

```powershell
az login
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:8000`, start the conversation, allow microphone access, and turn the microphone on. `DefaultAzureCredential` uses Azure CLI locally and managed identity when deployed.

## Architecture

The browser sends 24 kHz mono PCM16 microphone chunks to FastAPI over WebSocket. The server opens the Voice Live agent session with Entra credentials. Avatar video and audio stream directly from Voice Live to the browser over WebRTC; transcripts and state events return through the server WebSocket.

No Azure key or Entra token is exposed to browser JavaScript.
