import os
import random
import time
import threading
import pygame
import truck_telemetry

# ==================== SETTINGS ====================
BASE_FOLDER = "Dispatcher_Calls"
VOLUME = 0.93
BUZZ_VOLUME = 0.50
CHECK_INTERVAL = 1.8      # main tiered-logic loop
POLL_INTERVAL = 0.2       # fast poller, catches short-lived pulse events

SPEEDING_BUFFER_KMH = 15
FLIP_ROLL_THRESHOLD = 0.22    # tune once you get a real flip logged
HARD_BRAKE_THRESHOLD = 0.75   # userBrake pedal position, 0-1
SWERVE_STEER_THRESHOLD = 0.6
SWERVE_MIN_SPEED_KMH = 40
SWERVE_SUSTAIN_POLLS = 3      # steering must hold for this many polls in a row
ENABLE_FINE = False           # Fine detection disabled for now
ENABLE_TIRED = True           # restStop confirmed to correctly track hours-remaining fatigue
TIRED_REST_THRESHOLD = 130    # restStop below this counts as "getting tired"
REPAIR_WEAR_DROP = 0.02
DAMAGE_WEAR_JUMP = 0.008
SLEEP_REST_JUMP = 60
IDLE_TRIGGER_SECONDS = 180  # must be stopped this long before Idle_No_Job can fire

OFFROAD_SURFACES = {"dirt", "grass", "gravel", "sand", "mud", "road_dirt", "road_snow", "snow"}

COOLDOWN = {
    "Startup": 9999,
    "Damage_Taken": 45,
    "Fine": 40,
    "Flipped": 60,
    "Sleep": 90,
    "Arrival": 90,
    "Short_Job": 120,
    "Repair": 180,
    "Long_Job": 300,
    "Speeding": 70,
    "Offroad": 80,
    "High_Value_Job": 240,
    "Cargo_Damage": 120,
    "Vehicle_Wear": 180,
    "Low_Fuel": 150,
    "Tired": 160,
    "Night_Driving": 200,
    "Idle_No_Job": 100,
    "Jobs_Available": 140,
    "Rain": 180,
    "Sarcasm": 90,
    "Long_Drive": 200,
}
# ==================================================

pygame.mixer.init()
truck_telemetry.init()

BUZZ_FILE = os.path.join(BASE_FOLDER, "radio_buzz.wav")

state_lock = threading.Lock()

shared = {
    "data": {},
    "pending_fine": False,
    "pending_arrival": False,
    "pending_sleep": False,
    "pending_repair": False,
    "pending_damage": False,
    "was_on_job": False,
    "prev_wear": None,
    "prev_rest": 999,
    "last_seen_fine_amount": 0,
    "steer_hold_count": 0,
    "idle_since": None,
    "job_length_announced": False,
    "job_value_announced": False,
    "was_night": False,
    "night_announced": False,
    "was_raining": False,
    "rain_announced": False,
}


def get_sounds():
    sounds = {}
    for category in os.listdir(BASE_FOLDER):
        path = os.path.join(BASE_FOLDER, category)
        if os.path.isdir(path):
            files = sorted([os.path.join(path, f) for f in os.listdir(path) if f.endswith(".mp3")])
            if files:
                sounds[category] = files
    return sounds


def play_buzz():
    if os.path.exists(BUZZ_FILE):
        try:
            pygame.mixer.music.load(BUZZ_FILE)
            pygame.mixer.music.set_volume(BUZZ_VOLUME)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                time.sleep(0.02)
            time.sleep(0.12)
        except Exception:
            pass


def play(filepath):
    try:
        play_buzz()
        pygame.mixer.music.load(filepath)
        pygame.mixer.music.set_volume(VOLUME)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.04)
    except Exception as e:
        print("Play error:", e)


class ShuffleBag:
    """Plays every file in a category at least once before reshuffling."""
    def __init__(self, files):
        self.files = files[:]
        self.bag = []
        self.refill()

    def refill(self):
        self.bag = self.files[:]
        random.shuffle(self.bag)

    def next(self):
        if not self.bag:
            self.refill()
        return self.bag.pop()


def on_offroad_surface(data):
    substances = data.get("substances", [])
    wheel_subst = data.get("truck_wheelSubstance", [])
    if not substances or not wheel_subst:
        return False
    for idx in wheel_subst[:4]:
        if 0 <= idx < len(substances) and substances[idx] in OFFROAD_SURFACES:
            return True
    return False


def poller_thread():
    """Runs fast and independently of playback. Latches events the slower
    main loop could otherwise miss, and keeps `shared` updated with the
    freshest telemetry."""
    while True:
        try:
            data = truck_telemetry.get_data()
        except Exception:
            time.sleep(POLL_INTERVAL)
            continue

        with state_lock:
            shared["data"] = data

            if not data.get("sdkActive", False) or data.get("paused", True):
                time.sleep(POLL_INTERVAL)
                continue

            # ---- Fine: rising edge on the actual amount only.
            # 'fined' just mirrors the "police can fine you" game option
            # and stays True the whole session, so it's ignored. ----
            if ENABLE_FINE:
                fine_amount = data.get("fineAmount", 0) or 0
                if fine_amount > shared["last_seen_fine_amount"]:
                    shared["pending_fine"] = True
                shared["last_seen_fine_amount"] = fine_amount

            # ---- Arrival: on_job going True -> False is the real signal,
            # since jobFinished/jobDelivered may also just sit True.
            # False -> True (a new job accepted) resets the one-time
            # Short_Job / Long_Job announcement for that job. ----
            on_job = data.get("onJob", False)
            if shared["was_on_job"] and not on_job:
                shared["pending_arrival"] = True
            if on_job and not shared["was_on_job"]:
                shared["job_length_announced"] = False
                shared["job_value_announced"] = False
            shared["was_on_job"] = on_job

            # ---- Sleep wake (restStop jump) ----
            rest_stop = data.get("restStop", 999) or 999
            if rest_stop > shared["prev_rest"] + SLEEP_REST_JUMP:
                shared["pending_sleep"] = True
            shared["prev_rest"] = rest_stop

            # ---- Wear-based repair / sudden damage ----
            wear = (data.get("wearCabin", 0) or 0) + (data.get("wearChassis", 0) or 0) + \
                   (data.get("wearEngine", 0) or 0) + (data.get("wearWheels", 0) or 0)
            if shared["prev_wear"] is not None:
                if wear < shared["prev_wear"] - REPAIR_WEAR_DROP:
                    shared["pending_repair"] = True
                if wear > shared["prev_wear"] + DAMAGE_WEAR_JUMP:
                    shared["pending_damage"] = True
            shared["prev_wear"] = wear

            # ---- Sustained steering, for swerve detection ----
            speed_kmh = abs(data.get("speed", 0) or 0) * 3.6
            steer = data.get("userSteer", 0) or 0
            if abs(steer) > SWERVE_STEER_THRESHOLD and speed_kmh > SWERVE_MIN_SPEED_KMH:
                shared["steer_hold_count"] += 1
            else:
                shared["steer_hold_count"] = 0

            # ---- How long the truck has been stopped, for Idle_No_Job ----
            if speed_kmh < 1.5:
                if shared["idle_since"] is None:
                    shared["idle_since"] = time.time()
            else:
                shared["idle_since"] = None

            # ---- Night window: reset the "already announced" flag once
            # night ends, so the next night can announce again. ----
            game_time = data.get("time_abs", 0) or data.get("time", 0) or 0
            hour = (game_time // 60) % 24
            is_night = hour >= 22 or hour <= 5
            if shared["was_night"] and not is_night:
                shared["night_announced"] = False
            shared["was_night"] = is_night

            # ---- Rain onset: reset the "already announced" flag once
            # wipers go off, so the next rain shower can announce again. ----
            wipers = data.get("wipers", False)
            if shared["was_raining"] and not wipers:
                shared["rain_announced"] = False
            shared["was_raining"] = wipers

        time.sleep(POLL_INTERVAL)


def main():
    sounds = get_sounds()
    if not sounds:
        print("No sound files found!")
        return

    bags = {cat: ShuffleBag(files) for cat, files in sounds.items()}

    print("Final Dispatcher online (fineAmount-only, userBrake, sustained-swerve)")
    print("Loaded categories:", list(sounds.keys()))
    print("Listening...\n")

    threading.Thread(target=poller_thread, daemon=True).start()

    last_played = {cat: 0 for cat in COOLDOWN}
    startup_done = False
    low_fuel_warned = False

    def can_play(cat):
        return cat in bags and (time.time() - last_played.get(cat, 0)) >= COOLDOWN.get(cat, 120)

    def trigger(cat):
        sound = bags[cat].next()
        print(f"-> [{cat}]")
        play(sound)
        last_played[cat] = time.time()

    while True:
        time.sleep(CHECK_INTERVAL)

        with state_lock:
            data = dict(shared["data"])
            pending_fine = shared["pending_fine"]
            pending_arrival = shared["pending_arrival"]
            pending_sleep = shared["pending_sleep"]
            pending_repair = shared["pending_repair"]
            pending_damage = shared["pending_damage"]
            steer_hold_count = shared["steer_hold_count"]
            idle_since = shared["idle_since"]
            rest_stop = shared["prev_rest"]
            job_length_announced = shared["job_length_announced"]
            job_value_announced = shared["job_value_announced"]
            night_announced = shared["night_announced"]
            rain_announced = shared["rain_announced"]

        if not data or not data.get("sdkActive", False) or data.get("paused", True):
            continue

        # ---------- Continuous values ----------
        speed_ms = abs(data.get("speed", 0) or 0)
        speed_kmh = speed_ms * 3.6

        speed_limit_ms = data.get("speedLimit", 0) or 0
        speed_limit_kmh = speed_limit_ms * 3.6

        on_job = data.get("onJob", False)
        cargo_damage = data.get("cargoDamage", 0) or 0
        wear = (data.get("wearCabin", 0) or 0) + (data.get("wearChassis", 0) or 0) + \
               (data.get("wearEngine", 0) or 0) + (data.get("wearWheels", 0) or 0)

        fuel = data.get("fuel", 100) or 100
        fuel_cap = data.get("fuelCapacity", 700) or 700
        fuel_percent = (fuel / fuel_cap) * 100 if fuel_cap > 0 else 100

        route_dist = data.get("routeDistance", 0) or 0
        job_income = data.get("jobIncome", 0) or 0

        roll = data.get("rotationZ", 0) or 0
        user_brake = data.get("userBrake", 0) or 0

        game_time = data.get("time_abs", 0) or data.get("time", 0) or 0
        wipers = data.get("wipers", False)

        offroad_now = on_offroad_surface(data)
        hard_brake = user_brake > HARD_BRAKE_THRESHOLD
        sharp_swerve = steer_hold_count >= SWERVE_SUSTAIN_POLLS
        bad_driving_event = hard_brake and sharp_swerve

        played_this_pass = False
        consumed = []

        # ============ HIGH PRIORITY ============

        if not played_this_pass and not startup_done and "Startup" in bags:
            trigger("Startup")
            startup_done = True
            played_this_pass = True

        if not played_this_pass and pending_damage and can_play("Damage_Taken"):
            trigger("Damage_Taken")
            played_this_pass = True
            consumed.append("pending_damage")

        if ENABLE_FINE and not played_this_pass and pending_fine and can_play("Fine"):
            trigger("Fine")
            played_this_pass = True
            consumed.append("pending_fine")

        if not played_this_pass and abs(roll) > FLIP_ROLL_THRESHOLD and can_play("Flipped"):
            trigger("Flipped")
            played_this_pass = True

        if not played_this_pass and pending_sleep and can_play("Sleep"):
            trigger("Sleep")
            played_this_pass = True
            consumed.append("pending_sleep")

        if not played_this_pass and pending_arrival and can_play("Arrival"):
            trigger("Arrival")
            played_this_pass = True
            consumed.append("pending_arrival")

        if not played_this_pass and on_job and not job_length_announced \
                and 0 < route_dist < 300000 and can_play("Short_Job"):
            trigger("Short_Job")
            played_this_pass = True
            with state_lock:
                shared["job_length_announced"] = True

        # ============ MEDIUM PRIORITY ============

        if not played_this_pass and pending_repair and can_play("Repair"):
            trigger("Repair")
            played_this_pass = True
            consumed.append("pending_repair")

        if not played_this_pass and on_job and not job_length_announced \
                and route_dist > 300000 and can_play("Long_Job"):
            trigger("Long_Job")
            played_this_pass = True
            with state_lock:
                shared["job_length_announced"] = True

        if not played_this_pass and speed_limit_kmh > 0 \
                and speed_kmh > speed_limit_kmh + SPEEDING_BUFFER_KMH and can_play("Speeding"):
            trigger("Speeding")
            played_this_pass = True

        if not played_this_pass and offroad_now and speed_kmh > 15 and can_play("Offroad"):
            trigger("Offroad")
            played_this_pass = True

        if not played_this_pass and bad_driving_event and can_play("Sarcasm"):
            trigger("Sarcasm")
            played_this_pass = True

        if not played_this_pass and on_job and not job_value_announced \
                and job_income > 16000 and can_play("High_Value_Job"):
            trigger("High_Value_Job")
            played_this_pass = True
            with state_lock:
                shared["job_value_announced"] = True

        # ============ LOW PRIORITY ============

        if not played_this_pass and cargo_damage > 0.12 and can_play("Cargo_Damage"):
            trigger("Cargo_Damage")
            played_this_pass = True

        if not played_this_pass and wear > 0.28 and can_play("Vehicle_Wear"):
            trigger("Vehicle_Wear")
            played_this_pass = True

        if not played_this_pass and fuel_percent < 16 and not low_fuel_warned and can_play("Low_Fuel"):
            trigger("Low_Fuel")
            low_fuel_warned = True
            played_this_pass = True
        if fuel_percent > 30:
            low_fuel_warned = False

        if ENABLE_TIRED and not played_this_pass and speed_kmh > 5 \
                and rest_stop < TIRED_REST_THRESHOLD and can_play("Tired"):
            trigger("Tired")
            played_this_pass = True

        if not played_this_pass and speed_kmh > 30 and not night_announced and can_play("Night_Driving"):
            hour = (game_time // 60) % 24
            if hour >= 22 or hour <= 5:
                trigger("Night_Driving")
                played_this_pass = True
                with state_lock:
                    shared["night_announced"] = True

        idle_long_enough = idle_since is not None and (time.time() - idle_since) >= IDLE_TRIGGER_SECONDS
        if not played_this_pass and not on_job and idle_long_enough and can_play("Idle_No_Job"):
            trigger("Idle_No_Job")
            played_this_pass = True

        if not played_this_pass and not on_job and speed_kmh > 25 and can_play("Jobs_Available") \
                and random.random() < 0.11:
            trigger("Jobs_Available")
            played_this_pass = True

        if not played_this_pass and wipers and not rain_announced and speed_kmh > 20 and can_play("Rain"):
            trigger("Rain")
            played_this_pass = True
            with state_lock:
                shared["rain_announced"] = True

        if not played_this_pass and speed_kmh > 50 and random.random() < 0.04 and can_play("Long_Drive"):
            trigger("Long_Drive")
            played_this_pass = True

        # ---------- Clear consumed pulse flags ----------
        if consumed:
            with state_lock:
                for flag in consumed:
                    shared[flag] = False


if __name__ == "__main__":
    main()