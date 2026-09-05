import os
import math
import random
import time
from pathlib import Path
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request

import requests
import json

env_path = Path('.') / '.env'
load_dotenv(dotenv_path=env_path)

app = App(token=os.environ["SLACK_TOKEN"], signing_secret=os.environ["SIGNING_SECRET"])
BOT_USER_ID = app.client.auth_test()["user_id"]
K_FACTOR = 32

flask_app = Flask(__name__)
handler = SlackRequestHandler(app)

@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    # automatically handle slack verification
    data = request.json
    if data and "challenge" in data:
        return {"challenge": data["challenge"]}
        
    return handler.handle(request)

KVDB_URL = os.environ.get("KVDB_URL")
DATA_KEY = "elo_data"

def load_elo_data():
    """Fetches the ELO rankings from the cloud bucket on startup."""
    try:
        if not KVDB_URL:
            return {}
        response = requests.get(f"{KVDB_URL}{DATA_KEY}")
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        print(f"Error loading cloud data: {e}")
    return {}

def save_elo_data(data):
    """Saves the entire ELO rankings dictionary back to the cloud."""
    try:
        if not KVDB_URL:
            return
        headers = {'Content-Type': 'application/json'}
        requests.post(f"{KVDB_URL}{DATA_KEY}", data=json.dumps(data), headers=headers)
    except Exception as e:
        print(f"Error saving data to cloud: {e}")

elo_storage = load_elo_data()
pending_votes = {}
match_queues = {}
active_match_votes = {} # voting for the winner

@app.event("message")
def handle_message_events(body, logger):
    # logger.info(body)
    
    # Extract the event object
    event = body.get("event", {})
    
    # Extract the user ID
    user_id = event.get("user")
    channel_id = event.get("channel")
    
    # Ignore authorless messsages
    if not user_id:
        return

    # check if bot is the sender
    if user_id != BOT_USER_ID:
        if not user_id in elo_storage:
            elo_storage[user_id] = 1000
            save_elo_data(elo_storage)

@app.command("/ranked-leaderboard") # displays the current leaderboard, ranked by elo
def leaderboard_display(ack, command, client):
    ack()

    channel_id = command.get('channel_id')
    user_id = command.get('user_id')

    if not elo_storage:
        client.chat_postMessage(
            channel=channel_id,
            text="*Ranked Leaderboard*\nNo players have registered an ELO rank yet!"
        )
        return

    # sorted() returns a list of tuples: [('U123', 1050), ('U456', 1000)]
    sorted_leaderboard = sorted(
        elo_storage.items(), 
        key=lambda item: item[1], 
        reverse=True
    )

    # leaderboard layout
    leaderboard_text = " *Top Sim Players - Ranked Leaderboard*\n"
    leaderboard_text += "‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾\n"
    
    # Add numerical placement symbols (🥇, 🥈, 🥉) for top spots
    for rank, (player_id, score) in enumerate(sorted_leaderboard, start=1):
        if rank == 1:
            medal = "🥇"
        elif rank == 2:
            medal = "🥈"
        elif rank == 3:
            medal = "🥉"
        else:
            medal = f" *{rank}.*" # Standard number for 4th place and below

        try:
            user_profile = client.users_info(user=player_id)
            display_name = user_profile["user"]["profile"]["real_name"]
        except Exception:
            # Fallback text if the user left the workspace or the API call fails
            display_name = f"User {player_id}"

        # Medal/Position - Ping User - ELO Score
        leaderboard_text += f"{medal} *{display_name}* — `{score} ELO`\n"

    client.chat_postMessage(
        channel=channel_id,
        text=leaderboard_text
    )



@app.command("/q-list")
def q_list(ack, command, client):
    ack()

    channel_id = command.get('channel_id')

    # fetch queue, default empty list if nonexistant
    current_queue = pending_votes.get(channel_id, [])
    
    if not current_queue:
        client.chat_postMessage(channel=channel_id, text="The queue is currently empty!")
    else:
        client.chat_postMessage(channel=channel_id, text=f"Current queue: {current_queue}")

# voting for desired game specs (pre-match)
@app.command("/q-voting")
def handle_q_voting(ack, command, client):
    ack()

    channel_id = command.get('channel_id')
    user_id = command.get('user_id')
    raw_text = command.get('text', '').strip() # arguments

    # multiple arguments (/q-voting <simulation type> <robot type>)
    args = raw_text.split()


    if not raw_text:
        # Handle case where user forgot to add arguments
        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="Invalid Command; Lacking Arguments! Ex: `/q-voting MoSim 4414`"
        )
        return

    if len(args) < 2:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text="Please provide the Simulation Type (Ex: MoSim, CloSim, xRC) and Robot Number (Ex: 4414, 1678, 1706) separated by a space for your vote"
            )
            return

    if channel_id not in pending_votes:
        pending_votes[channel_id] = []

    if any(submission["user"] == user_id for submission in pending_votes[channel_id]):
        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="Already submitted your arguments! Waiting for an opponent..."
        )
        return

    pending_votes[channel_id].append({
        "user": user_id,
        "args": args
    })

    current_count = len(pending_votes[channel_id])

    client.chat_postEphemeral(
        channel=channel_id, 
        user=user_id, 
        text=f"You have queued for *{args[0]}* {args[1]}")

    if len(pending_votes[channel_id]) == 1:
        client.chat_postMessage(
            channel=channel_id,
            text=f"*<@{user_id}> has queued!* Queue for Vote 1v1 is now *[{current_count}/2]*\nRun `/q-voting arg1 arg2` to enter!"
        )
        return

    # second user submits, picking match specs
    if len(pending_votes[channel_id]) == 2:
        player1 = pending_votes[channel_id][0]
        player2 = pending_votes[channel_id][1]

        match_vote = random.randint(0, 1)
        
        if match_vote == 0:
            winner = player1
            loser = player2
        else:
            winner = player2
            loser = player1

        # get scores with default value 1000 if none exists
        winner_elo = elo_storage.get(winner['user'], 1000)
        loser_elo = elo_storage.get(loser['user'], 1000)

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

        # post match stuff starts here
        winner_profile = client.users_info(user=winner['user'])
        winner_name = winner_profile["user"]["profile"]["real_name"]

        loser_profile = client.users_info(user=loser['user'])
        loser_name = loser_profile["user"]["profile"]["real_name"]

        # ids are used to ping the winner/loser
        winner_id = winner['user']
        loser_id = loser['user']

        # three button array
        confirmation_blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Vote for the Winner of the Match*\n 🟥 *Red:* <@{winner_id}>\n 🟦 *Blue:* <@{loser_id}>\n\n"
                }
            },
            {
                "type": "actions",
                "block_id": "match_confirmation_zone",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": f"Confirm {winner_name} win",
                            "emoji": True
                        },
                        "style": "primary",
                        "value": f"{winner_id}_{loser_id}",
                        "action_id": "confirm_winner_button"
                    },
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": f"Confirm {loser_name} win",
                            "emoji": True
                        },
                        "value": f"{loser_id}_{winner_id}",
                        "action_id": "confirm_loser_button"
                    }
                ]
            }
        ]

        # delivering the layout confirmation block
        client.chat_postMessage(
            channel=channel_id,
            text="Awaiting verification confirmations...",
            blocks=confirmation_blocks
        )

        del pending_votes[channel_id]

def calculate_dynamic_elo(player_elo, opponent_elo, actual_score):


    """
    actual_score is always 1 for a win, 0 for a loss.
    returns elo adjustment value of any real number
    """
    # 1. Calculate Expected Win Probability for the player
    expected_prb = 1 / (1 + math.pow(10, (opponent_elo - player_elo) / 400))
    
    # 2. Calculate point difference outcome scale
    elo_adjustment = round(K_FACTOR * (actual_score - expected_prb), 1)
    return elo_adjustment

# handles confirm <> won button
@app.action("confirm_winner_button")
def handle_winner_confirmation(ack, body, client):
    ack()
    clicking_user = body["user"]["id"]
    channel_id = body["channel"]["id"]
    message_ts = body["message"]["ts"]
    winner_id, loser_id = body["actions"][0]["value"].split("_")

    if clicking_user not in [winner_id, loser_id]:
        client.chat_postEphemeral(channel=channel_id, user=clicking_user, text="You are an external player of this match!")
        return

    if message_ts not in active_match_votes:
        active_match_votes[message_ts] = {"users": []}
        
    if clicking_user in active_match_votes[message_ts]["users"]:
        client.chat_postEphemeral(channel=channel_id, user=clicking_user, text="Waiting on opponent confirmation click...")
        return
        
    active_match_votes[message_ts]["users"].append(clicking_user)


    # Execute math adjustments once both players approve
    if len(active_match_votes[message_ts]["users"]) == 2:
        # Fetch current ELO ratings
        w_current = elo_storage.get(winner_id, 1000)
        l_current = elo_storage.get(loser_id, 1000)

        # dynamic elo shifts
        w_change = calculate_dynamic_elo(w_current, l_current, actual_score=1.0) # Win outcome
        l_change = calculate_dynamic_elo(l_current, w_current, actual_score=0.0) # Loss outcome

        # update global database dict fields
        elo_storage[winner_id] = w_current + w_change
        elo_storage[loser_id] = max(0, l_current + l_change) # Enforce floor limit boundary

        save_elo_data(elo_storage)

        client.chat_update(
            channel=channel_id,
            ts=message_ts,
            blocks=[{
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Match Results Verified!*\n"
                        f"🟥 *Red:* <@{winner_id}> wins! (`+{w_change}` ELO) | `{elo_storage[winner_id]}`\n"
                        f"🟦 *Blue:* <@{loser_id}> loses! (`{l_change}` ELO) | `{elo_storage[loser_id]}`"
                    )
                }
            }]
        )
        del active_match_votes[message_ts]

# handles specs loser win
@app.action("confirm_loser_button")
def handle_loser_confirmation(ack, body, client):
    ack()
    clicking_user = body["user"]["id"]
    channel_id = body["channel"]["id"]
    message_ts = body["message"]["ts"]
    actual_winner_id, actual_loser_id = body["actions"][0]["value"].split("_")

    if clicking_user not in [actual_winner_id, actual_loser_id]:
        client.chat_postEphemeral(channel=channel_id, user=clicking_user, text="You are an external player of this match!")
        return

    if message_ts not in active_match_votes:
        active_match_votes[message_ts] = {"users": []}
        
    if clicking_user in active_match_votes[message_ts]["users"]:
        client.chat_postEphemeral(channel=channel_id, user=clicking_user, text="Waiting on opponent confirmation click...")
        return
        
    active_match_votes[message_ts]["users"].append(clicking_user)

    if len(active_match_votes[message_ts]["users"]) == 2:
        w_current = elo_storage.get(actual_winner_id, 1000)
        l_current = elo_storage.get(actual_loser_id, 1000)

        w_change = calculate_dynamic_elo(w_current, l_current, actual_score=1.0)
        l_change = calculate_dynamic_elo(l_current, w_current, actual_score=0.0)

        elo_storage[actual_winner_id] = w_current + w_change
        elo_storage[actual_loser_id] = max(0, l_current + l_change)

        save_elo_data(elo_storage)

        client.chat_update(
            channel=channel_id,
            ts=message_ts,
            blocks=[{
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Match Results Verified!*\n"
                        f"🟥 *Red:* <@{actual_winner_id}> wins! (`+{w_change}` ELO) | `{elo_storage[actual_winner_id]}`\n"
                        f"🟦 *Blue:* <@{actual_loser_id}> loses! (`{l_change}` ELO) | `{elo_storage[actual_loser_id]}`"
                    )
                }
            }]
        )
        del active_match_votes[message_ts]


@app.command("/leaveall")
def leaveall(ack, command, client):
    ack()

    channel_id = command.get('channel_id')
    user_id = command.get('user_id')

    # checks if command is executable in current channel
    if channel_id not in pending_votes or not pending_votes[channel_id]:
        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="No queues to leave from"
        )
        return

    # checks if user is in a queue for this channel
    user_in_queue = any(submission["user"] == user_id for submission in pending_votes[channel_id])

    if not user_in_queue:
        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="You are not currently waiting in this queue"
        )
        return

    # recreates queue without the user's entry
    pending_votes[channel_id] = [
        submission for submission in pending_votes[channel_id] 
        if submission["user"] != user_id
    ]

    current_count = len(pending_votes[channel_id]) # counting for the updated queue size

    # broadcast queue status change
    client.chat_postMessage(
        channel=channel_id, 
        text=f"*<@{user_id}> has left the queue.* Queue for Vote 1v1 is now *[{current_count}/2]*\nRun `/q-voting arg1 arg2` to enter!"
    )


if __name__ == "__main__":
    # render binds dynamic port ranges automatically
    port = int(os.environ.get("PORT", 3000))
    flask_app.run(host="0.0.0.0", port=port)
