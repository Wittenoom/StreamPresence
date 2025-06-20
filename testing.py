from DiscordRPCWrapper import DiscordRPCWrapper
import time

rpc = DiscordRPCWrapper("1383823713119506543")

@rpc.hook("on_ready")
def ready():
    print("Discord RPC is ready!")

@rpc.hook("on_error")
def error(e):
    print(f"Discord RPC error: {e}")

rpc.start()

rpc.update_presence(
)

try:
    while True:
        time.sleep(10)
except KeyboardInterrupt:
    rpc.stop()
    print("RPC stopped.")
