import os
import math
import random
from pathlib import Path
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, db
from slack_bolt import App
from slack_bolt.adapter.google_cloud_functions import SlackRequestHandler

env_path = Path('.') / '.env'
load_dotenv(dotenv_path=env_path)

# initialize fb admin sdk
if not firebase_admin._apps:
    cred_path = os.environ.get("FIREBASE_CREDENTIALS_PATH")
    db_url = os.environ.get("FIREBASE_DB_URL")  

    if cred_path and Path(cred_path).exists():
        # local dev mode
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred, {
            'databaseURL': db_url
        })
    else:
        # google cloud mode
        firebase_admin.initialize_app(options={
            'databaseURL': db_url
        })

app = App(token=os.environ["SLACK_TOKEN"], signing_secret=os.environ["SIGNING_SECRET"], process_before_response=False)
BOT_USER_ID = app.client.auth_test()["user_id"]
K_FACTOR = 32
PROJECT_FOLDER = "slack-sim-bot"

handler = SlackRequestHandler(app)

def slack_bot_serverless(request):
    """
    All previous instance interaction handled with firebase storage. this acts as conduit for all info
    """
    return handler.handle(request)

# all the firebase getting/setting stuff
def get_user_elo(user_id):
    ref = db.reference(f'{PROJECT_FOLDER}/elo_storage/{user_id}')
    score = ref.get()
    return score if score is not None else 1000

def set_user_elo(user_id, score):
    db.reference(f'{PROJECT_FOLDER}/elo_storage/{user_id}').set(score)

def calculate_dynamic_elo(player_elo, opponent_elo, actual_score):
    expected_prb = 1 / (1 + math.pow(10, (opponent_elo - player_elo) / 400))
    elo_adjustment = round(K_FACTOR * (actual_score - expected_prb), 1)
    return elo_adjustment

def ack_leaderboard(ack):
    ack()

def process_leaderboard(command, client):
    channel_id = command.get('channel_id')
    
    # read elo directly from fb storage bucket
    elo_storage = db.reference(f'{PROJECT_FOLDER}/elo_storage').get() or {}

    if not elo_storage:
        client.chat_postMessage(channel=channel_id, text="*Ranked Leaderboard*\nNo ranked players yet!")
        return

    sorted_leaderboard = sorted(elo_storage.items(), key=lambda item: item[1], reverse=True)
    leaderboard_text = " *Top Sim Players – Ranked Leaderboard*\n‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾\n"
    
    for rank, (player_id, score) in enumerate(sorted_leaderboard, start=1):
        medal = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else f" *{rank}.*"
        try:
            display_name = client.users_info(user=player_id)["user"]["profile"]["real_name"]
        except Exception:
            display_name = f"User {player_id}"
        leaderboard_text += f"{medal} *{display_name}* — `{score} ELO`\n"

    client.chat_postMessage(channel=channel_id, text=leaderboard_text)

app.command("/ranked-leaderboard")(ack=ack_leaderboard, lazy=[process_leaderboard])

def ack_q_voting(ack):
    ack()

def process_q_voting(command, client):
    channel_id = command.get('channel_id')
    user_id = command.get('user_id')
    args = command.get('text', '').strip().split()

    if len(args) < 2:
        client.chat_postEphemeral(channel=channel_id, user=user_id, text="Lacking Arguments! Ex: `/q-voting MoSim 4414`")
        return

    queue_ref = db.reference(f'{PROJECT_FOLDER}/pending_votes/{channel_id}')
    current_queue = queue_ref.get() or []

    if any(sub["user"] == user_id for sub in current_queue):
        client.chat_postEphemeral(channel=channel_id, user=user_id, text="Already waiting in queue...")
        return

    current_queue.append({"user": user_id, "args": args})
    queue_ref.set(current_queue)
    
    client.chat_postEphemeral(channel=channel_id, user=user_id, text=f"You queued for *{args[0]}* ({args[1]})")

    if len(current_queue) == 1:
        client.chat_postMessage(
            channel=channel_id,
            text=f"*<@{user_id}> has queued!* Queue is now *[1/2]*\nRun `/q-voting arg1 arg2` to enter!"
        )
        return

    elif len(current_queue) == 2:
        player1, player2 = current_queue[0], current_queue[1]
        winner, loser = (player1, player2) if random.randint(0, 1) == 0 else (player2, player1)

        winner_elo = get_user_elo(winner['user'])
        loser_elo = get_user_elo(loser['user'])

        client.chat_postMessage(
            channel=channel_id,
            text=(
                f"*Match Info*\n\n"
                f"🟥 *Red:* <@{winner['user']}>  `{winner_elo}`\n"
                f"🟦 *Blue:* <@{loser['user']}>  `{loser_elo}`\n"
                f"The randomly selected game is: *{winner['args'][0]}* ({winner['args'][1]})\n\n"
                f"A winner must be agreed upon by *both players* post-match."
            )
        )

        winner_name = client.users_info(user=winner['user'])["user"]["profile"]["real_name"]
        loser_name = client.users_info(user=loser['user'])["user"]["profile"]["real_name"]

        confirmation_blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Vote for the Winner*\n 🟥 *Red:* <@{winner['user']}>\n 🟦 *Blue:* <@{loser['user']}>\n\n"}
            },
            {
                "type": "actions",
                "block_id": "match_confirmation_zone",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": f"Confirm {winner_name} win"}, "style": "primary", "value": f"{winner['user']}_{loser['user']}", "action_id": "confirm_winner_button"},
                    {"type": "button", "text": {"type": "plain_text", "text": f"Confirm {loser_name} win"}, "value": f"{loser['user']}_{winner['user']}", "action_id": "confirm_loser_button"}
                ]
            }
        ]

        client.chat_postMessage(channel=channel_id, text="Awaiting confirmations...", blocks=confirmation_blocks)
        
        queue_ref.delete()
        return

app.command("/q-voting")(ack=ack_q_voting, lazy=[process_q_voting])

def ack_action(ack):
    ack()

def process_confirmation(body, client):
    clicking_user = body["user"]["id"]
    channel_id = body["channel"]["id"]
    message_ts = body["message"]["ts"]
    winner_id, loser_id = body["actions"][0]["value"].split("_")

    if clicking_user not in [winner_id, loser_id]:
        client.chat_postEphemeral(channel=channel_id, user=clicking_user, text="You are not a player in this match!")
        return

    # Track votes on Firebase
    vote_ref = db.reference(f'{PROJECT_FOLDER}/active_match_votes/{message_ts}')

    def append_user_transaction(current_list):
        if current_list is None:
            current_list = []
        if clicking_user not in current_list:
            current_list.append(clicking_user)
        return current_list

    try:
        # automatically runs inside fb
        updated_users = vote_ref.transaction(append_user_transaction)
    except Exception as e:
        client.chat_postEphemeral(channel=channel_id, user=clicking_user, text="Request was not fulfilled. Please click again!")
        return

    if updated_users.count(clicking_user) > 1:
         client.chat_postEphemeral(channel=channel_id, user=clicking_user, text="Waiting on opponent confirmation...")
         return

    if len(updated_users) == 2:
        w_current = get_user_elo(winner_id)
        l_current = get_user_elo(loser_id)

        w_change = calculate_dynamic_elo(w_current, l_current, actual_score=1.0)
        l_change = calculate_dynamic_elo(l_current, w_current, actual_score=0.0)

        set_user_elo(winner_id, w_current + w_change)
        set_user_elo(loser_id, max(0, l_current + l_change))

        client.chat_update(
            channel=channel_id,
            ts=message_ts,
            blocks=[{
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Match Results Verified!*\n🟥 <@{winner_id}> wins! (`+{w_change}`) | `{w_current + w_change}`\n🟦 <@{loser_id}> loses! (`{l_change}`) | `{max(0, l_current + l_change)}`"
                }
            }]
        )
        vote_ref.delete()

app.action("confirm_winner_button")(ack=ack_action, lazy=[process_confirmation])
app.action("confirm_loser_button")(ack=ack_action, lazy=[process_confirmation])

def ack_q_list(ack):
    ack()

def process_q_list(command, client):
    channel_id = command.get('channel_id')

    # queue state read from firebase
    current_queue = db.reference(f'{PROJECT_FOLDER}/pending_votes/{channel_id}').get() or []
    
    if not current_queue:
        client.chat_postMessage(channel=channel_id, text="The queue is currently empty!")
    else:
        queue_msg = "*Active Queue:*\n"
        for index, item in enumerate(current_queue, start=1):
            queue_msg += f"{index}. <@{item['user']}> — `{item['args'][0]}` ({item['args'][1]})\n"
        client.chat_postMessage(channel=channel_id, text=queue_msg)

app.command("/q-list")(ack=ack_q_list, lazy=[process_q_list])

def ack_leaveall(ack):
    ack()

def process_leaveall(command, client):
    channel_id = command.get('channel_id')
    user_id = command.get('user_id')
    
    # fb lookup
    queue_ref = db.reference(f'{PROJECT_FOLDER}/pending_votes/{channel_id}')
    current_queue = queue_ref.get() or []

    # check if no channel queue
    if not current_queue:
        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="No queues to leave from"
        )
        return

    # check if user in queue
    user_in_queue = any(submission["user"] == user_id for submission in current_queue)
    if not user_in_queue:
        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="You are not currently waiting in this queue"
        )
        return

    # update firebase for player who left
    updated_queue = [sub for sub in current_queue if sub["user"] != user_id]

    if not updated_queue:
        queue_ref.delete()
    else:
        queue_ref.set(updated_queue)

    current_count = len(updated_queue)

    # updated status to channel
    client.chat_postMessage(
        channel=channel_id, 
        text=f"*<@{user_id}> has left the queue.* Queue for Vote 1v1 is now *[{current_count}/2]*\nRun `/q-voting arg1 arg2` to enter!"
    )

app.command("/leaveall")(ack=ack_leaveall, lazy=[process_leaveall])