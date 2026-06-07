"""Proxmox Auto VM Bot — create Proxmox VMs from Discord.

/promox -> pick Node / OS / RAM / Disk / free IP -> set name+user+pass
-> clones the cloud-init template, configures it, and boots a ready VM.
"""

import asyncio
import os
import subprocess
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv
from proxmoxer import ProxmoxAPI

load_dotenv(os.environ.get("ENV_FILE", Path(__file__).with_name(".env")))

TOKEN = os.environ["DISCORD_TOKEN"]
PVE_HOST = os.environ["PVE_HOST"]
TOKEN_ID = os.environ["PVE_TOKEN_ID"]
TOKEN_SECRET = os.environ["PVE_TOKEN_SECRET"]
TEMPLATE_NODE = os.environ["PVE_TEMPLATE_NODE"]
TEMPLATE_ID = int(os.environ.get("PVE_TEMPLATE_ID", "9000"))
STORAGE = os.environ["PVE_STORAGE"]
GATEWAY = os.environ["GATEWAY"]
NAMESERVER = os.environ["NAMESERVER"]
PREFIX = os.environ["IP_PREFIX"]

# OS templates (extend as more cloud-init templates are added).
OS_TEMPLATES = {
    "ubuntu-2404": ("Ubuntu 24.04", 9000),
    "debian-13": ("Debian 13", 9001),
    "alpine": ("Alpine (low RAM)", 9002),
}
CT_STORAGE = os.environ.get("CT_STORAGE", STORAGE)
CT_BRIDGE = os.environ.get("CT_BRIDGE", "vmbr0")
CT_TEMPLATES = {
    key: (label, template)
    for key, label, template in [
        ("debian-12", "Debian 12 CT", os.environ.get("CT_TEMPLATE_DEBIAN_12", "")),
        ("alpine-323", "Alpine 3.23 CT", os.environ.get("CT_TEMPLATE_ALPINE_323", "")),
    ]
    if template
}

def _parse_host_ranges(value: str) -> list[int]:
    hosts: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            hosts.extend(range(int(start), int(end) + 1))
        else:
            hosts.append(int(part))
    return hosts


IP_EXCLUDE = {int(h) for h in os.environ.get("IP_EXCLUDE_HOSTS", "").split(",") if h.strip()}
IP_HOSTS = [h for h in _parse_host_ranges(os.environ["IP_HOST_RANGES"]) if h not in IP_EXCLUDE]


def pve() -> ProxmoxAPI:
    user, name = TOKEN_ID.split("!", 1)
    return ProxmoxAPI(
        PVE_HOST, user=user, token_name=name, token_value=TOKEN_SECRET,
        verify_ssl=False,
    )


# ---------------------------------------------------------------- free IPs ---
async def _ping(ip: str) -> bool:
    p = await asyncio.create_subprocess_exec(
        "ping", "-c1", "-W1", ip,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    return (await p.wait()) == 0


def _arp_in_use() -> set[str]:
    out = subprocess.run(["ip", "neigh"], capture_output=True, text=True).stdout
    used = set()
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        ip = parts[0]
        if "lladdr" in line and "FAILED" not in line and "INCOMPLETE" not in line:
            used.add(ip)
    return used


async def free_ips() -> list[str]:
    ips = [f"{PREFIX}.{h}" for h in IP_HOSTS]
    alive = await asyncio.gather(*[_ping(ip) for ip in ips])
    arp = _arp_in_use()
    return [ip for ip, up in zip(ips, alive) if not up and ip not in arp]


# ------------------------------------------------------------ Proxmox ops ---
def _nodes() -> list[str]:
    return sorted(n["node"] for n in pve().nodes.get() if n.get("status") == "online")


def _wait_task(px, node, upid, timeout=180):
    import time
    end = time.time() + timeout
    while time.time() < end:
        st = px.nodes(node).tasks(upid).status.get()
        if st["status"] == "stopped":
            if st.get("exitstatus") != "OK":
                raise RuntimeError(f"task failed: {st.get('exitstatus')}")
            return
        time.sleep(2)
    raise RuntimeError("task timed out")


def create_vm(node, template_id, ram, disk, ip, hostname, user, password) -> int:
    px = pve()
    vmid = int(px.cluster.nextid.get())
    upid = px.nodes(TEMPLATE_NODE).qemu(template_id).clone.post(
        newid=vmid, name=hostname, full=1, target=node, storage=STORAGE,
    )
    task_node = upid.split(":")[1] if upid.startswith("UPID:") else TEMPLATE_NODE
    _wait_task(px, task_node, upid)
    px.nodes(node).qemu(vmid).config.post(
        memory=ram, ciuser=user, cipassword=password,
        ipconfig0=f"ip={ip}/24,gw={GATEWAY}", nameserver=NAMESERVER,
    )
    px.nodes(node).qemu(vmid).resize.put(disk="scsi0", size=f"{disk}G")
    px.nodes(node).qemu(vmid).status.start.post()
    return vmid


def create_ct(node, template, ram, disk, ip, hostname, password) -> int:
    px = pve()
    vmid = int(px.cluster.nextid.get())
    upid = px.nodes(node).lxc.post(
        vmid=vmid,
        ostemplate=template,
        hostname=hostname,
        storage=CT_STORAGE,
        rootfs=f"{CT_STORAGE}:{disk}",
        memory=ram,
        swap=512,
        cores=1,
        password=password,
        unprivileged=1,
        features="nesting=1",
        nameserver=NAMESERVER,
        net0=f"name=eth0,bridge={CT_BRIDGE},ip={ip}/24,gw={GATEWAY}",
        start=1,
    )
    task_node = upid.split(":")[1] if upid.startswith("UPID:") else node
    _wait_task(px, task_node, upid)
    return vmid


# ------------------------------------------------------------- Discord UI ---
class CreateModal(discord.ui.Modal, title="Create VM"):
    def __init__(self, view: "VMView"):
        super().__init__()
        self.view_ref = view
        self.host = discord.ui.TextInput(label="Hostname", placeholder="my-vm", max_length=40)
        self.user = discord.ui.TextInput(label="Username", default="student", max_length=32)
        self.pw = discord.ui.TextInput(label="Password", min_length=6, max_length=64)
        self.ram = discord.ui.TextInput(
            label="RAM (MB)", default="1024",
            placeholder="เช่น 512 / 1024 / 2048 / 4096", max_length=6)
        self.disk = discord.ui.TextInput(
            label="Disk (GB)", default="20", placeholder="เช่น 20 / 40 / 80", max_length=4)
        for f in (self.host, self.user, self.pw, self.ram, self.disk):
            self.add_item(f)

    async def on_submit(self, itx: discord.Interaction):
        v = self.view_ref
        if not v.node or not v.ip:
            await itx.response.send_message("⚠️ เลือก Node และ IP ก่อน", ephemeral=True)
            return
        try:
            ram_mb = int(str(self.ram))
            disk_gb = int(str(self.disk))
            if not (256 <= ram_mb <= 65536):
                raise ValueError("RAM ต้องอยู่ 256–65536 MB")
            if not (3 <= disk_gb <= 500):
                raise ValueError("Disk ต้องอยู่ 3–500 GB")
        except ValueError as e:
            await itx.response.send_message(f"⚠️ ค่าไม่ถูกต้อง: {e}", ephemeral=True)
            return

        await itx.response.defer(ephemeral=True, thinking=True)
        try:
            vmid = await asyncio.to_thread(
                create_vm, v.node, v.template_id, ram_mb, disk_gb, v.ip,
                str(self.host), str(self.user), str(self.pw),
            )
        except Exception as e:
            await itx.followup.send(f"❌ สร้างไม่สำเร็จ: `{e}`", ephemeral=True)
            return
        os_name = OS_TEMPLATES[v.os_key][0]
        await itx.followup.send(
            f"✅ **สร้าง VM สำเร็จ!**\n"
            f"```\n"
            f"VMID     : {vmid}\n"
            f"Hostname : {self.host}\n"
            f"Node     : {v.node}\n"
            f"OS       : {os_name}\n"
            f"RAM/Disk : {ram_mb} MB / {disk_gb} GB\n"
            f"IP       : {v.ip}\n"
            f"User     : {self.user}\n"
            f"Password : {self.pw}\n"
            f"SSH      : ssh {self.user}@{v.ip}\n"
            f"```",
            ephemeral=True,
        )


class CreateCTModal(discord.ui.Modal, title="Create CT"):
    def __init__(self, view: "CTView"):
        super().__init__()
        self.view_ref = view
        self.host = discord.ui.TextInput(label="Hostname", placeholder="my-ct", max_length=40)
        self.pw = discord.ui.TextInput(label="Root Password", min_length=6, max_length=64)
        self.ram = discord.ui.TextInput(
            label="RAM (MB)", default="512",
            placeholder="เช่น 512 / 1024 / 2048", max_length=6)
        self.disk = discord.ui.TextInput(
            label="Disk (GB)", default="8", placeholder="เช่น 8 / 16 / 32", max_length=4)
        for f in (self.host, self.pw, self.ram, self.disk):
            self.add_item(f)

    async def on_submit(self, itx: discord.Interaction):
        v = self.view_ref
        if not v.node or not v.ip:
            await itx.response.send_message("⚠️ เลือก Node และ IP ก่อน", ephemeral=True)
            return
        try:
            ram_mb = int(str(self.ram))
            disk_gb = int(str(self.disk))
            if not (128 <= ram_mb <= 65536):
                raise ValueError("RAM ต้องอยู่ 128-65536 MB")
            if not (2 <= disk_gb <= 500):
                raise ValueError("Disk ต้องอยู่ 2-500 GB")
        except ValueError as e:
            await itx.response.send_message(f"⚠️ ค่าไม่ถูกต้อง: {e}", ephemeral=True)
            return

        await itx.response.defer(ephemeral=True, thinking=True)
        try:
            vmid = await asyncio.to_thread(
                create_ct, v.node, v.template, ram_mb, disk_gb, v.ip,
                str(self.host), str(self.pw),
            )
        except Exception as e:
            await itx.followup.send(f"❌ สร้าง CT ไม่สำเร็จ: `{e}`", ephemeral=True)
            return
        os_name = CT_TEMPLATES[v.os_key][0]
        await itx.followup.send(
            f"✅ **สร้าง CT สำเร็จ!**\n"
            f"```\n"
            f"CTID     : {vmid}\n"
            f"Hostname : {self.host}\n"
            f"Node     : {v.node}\n"
            f"OS       : {os_name}\n"
            f"RAM/Disk : {ram_mb} MB / {disk_gb} GB\n"
            f"IP       : {v.ip}\n"
            f"User     : root\n"
            f"Password : {self.pw}\n"
            f"SSH      : ssh root@{v.ip}\n"
            f"```",
            ephemeral=True,
        )


class _Select(discord.ui.Select):
    def __init__(self, view, attr, placeholder, options, cast=str):
        self._attr = attr
        self._cast = cast
        super().__init__(placeholder=placeholder, options=options, min_values=1, max_values=1)

    async def callback(self, itx: discord.Interaction):
        setattr(self.view, self._attr, self._cast(self.values[0]))
        if self._attr == "os_key":
            if hasattr(self.view, "template_id"):
                self.view.template_id = OS_TEMPLATES[self.view.os_key][1]
            if hasattr(self.view, "template"):
                self.view.template = CT_TEMPLATES[self.view.os_key][1]
        await itx.response.defer()


class VMView(discord.ui.View):
    def __init__(self, nodes: list[str], ips: list[str]):
        super().__init__(timeout=300)
        self.node = nodes[0] if nodes else None
        self.os_key = "ubuntu-2404"
        self.template_id = TEMPLATE_ID
        self.ram = 2048
        self.disk = 20
        self.ip = ips[0] if ips else None

        self.add_item(_Select(self, "node", "เลือก Node",
            [discord.SelectOption(label=n, default=(n == self.node)) for n in nodes]))
        self.add_item(_Select(self, "os_key", "เลือก OS",
            [discord.SelectOption(label=v[0], value=k, default=(k == self.os_key))
             for k, v in OS_TEMPLATES.items()]))
        if ips:
            self.add_item(_Select(self, "ip", "เลือก IP ว่าง",
                [discord.SelectOption(label=ip, default=(ip == self.ip)) for ip in ips[:25]]))

    @discord.ui.button(label="ตั้งชื่อ & สร้าง", style=discord.ButtonStyle.success, row=4)
    async def create(self, itx: discord.Interaction, _btn: discord.ui.Button):
        await itx.response.send_modal(CreateModal(self))


class CTView(discord.ui.View):
    def __init__(self, nodes: list[str], ips: list[str]):
        super().__init__(timeout=300)
        self.node = nodes[0] if nodes else None
        self.os_key = next(iter(CT_TEMPLATES), None)
        self.template = CT_TEMPLATES[self.os_key][1] if self.os_key else None
        self.ip = ips[0] if ips else None

        self.add_item(_Select(self, "node", "เลือก Node",
            [discord.SelectOption(label=n, default=(n == self.node)) for n in nodes]))
        self.add_item(_Select(self, "os_key", "เลือก CT OS",
            [discord.SelectOption(label=v[0], value=k, default=(k == self.os_key))
             for k, v in CT_TEMPLATES.items()]))
        if ips:
            self.add_item(_Select(self, "ip", "เลือก IP ว่าง",
                [discord.SelectOption(label=ip, default=(ip == self.ip)) for ip in ips[:25]]))

    @discord.ui.button(label="ตั้งชื่อ & สร้าง CT", style=discord.ButtonStyle.success, row=4)
    async def create(self, itx: discord.Interaction, _btn: discord.ui.Button):
        await itx.response.send_modal(CreateCTModal(self))


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()


client = Bot()


@client.tree.command(name="promox", description="สร้าง VM บน Proxmox (Satit-M)")
async def promox(itx: discord.Interaction):
    await itx.response.defer(ephemeral=True, thinking=True)
    try:
        nodes = await asyncio.to_thread(_nodes)
        ips = await free_ips()
    except Exception as e:
        await itx.followup.send(f"❌ ต่อ Proxmox ไม่ได้: `{e}`", ephemeral=True)
        return
    if not ips:
        await itx.followup.send("⚠️ ไม่มี IP ว่างใน pool ตอนนี้", ephemeral=True)
        return
    view = VMView(nodes, ips)
    await itx.followup.send(
        f"**Create a new VM** — choose values then click **Name & Create**\n"
        f"Free IPs: {len(ips)}",
        view=view, ephemeral=True,
    )


@client.tree.command(name="ip", description="แสดง IP ว่าง")
async def ip(itx: discord.Interaction):
    await itx.response.defer(ephemeral=True, thinking=True)
    try:
        ips = await free_ips()
    except Exception as e:
        await itx.followup.send(f"❌ ตรวจ IP ไม่ได้: `{e}`", ephemeral=True)
        return
    if not ips:
        await itx.followup.send("⚠️ ไม่มี IP ว่างใน pool ตอนนี้", ephemeral=True)
        return
    await itx.followup.send(
        "**Free IPs**\n"
        f"```\n{chr(10).join(ips)}\n```",
        ephemeral=True,
    )


@client.tree.command(name="ct", description="สร้าง LXC CT บน Proxmox")
async def ct(itx: discord.Interaction):
    await itx.response.defer(ephemeral=True, thinking=True)
    if not CT_TEMPLATES:
        await itx.followup.send("⚠️ ยังไม่ได้ตั้งค่า CT template", ephemeral=True)
        return
    try:
        nodes = await asyncio.to_thread(_nodes)
        ips = await free_ips()
    except Exception as e:
        await itx.followup.send(f"❌ ต่อ Proxmox ไม่ได้: `{e}`", ephemeral=True)
        return
    if not ips:
        await itx.followup.send("⚠️ ไม่มี IP ว่างใน pool ตอนนี้", ephemeral=True)
        return
    view = CTView(nodes, ips)
    await itx.followup.send(
        f"**Create a new CT** — choose values then click **Name & Create CT**\n"
        f"Free IPs: {len(ips)}",
        view=view, ephemeral=True,
    )


@client.event
async def on_ready():
    # Per-guild sync = commands show up instantly in every server the bot is in.
    for g in client.guilds:
        try:
            client.tree.copy_global_to(guild=g)
            await client.tree.sync(guild=g)
            print(f"synced /promox to guild {g.name} ({g.id})")
        except Exception as e:
            print(f"guild sync failed for {g.id}: {e}")
    print(f"logged in as {client.user}; guilds={[g.name for g in client.guilds]}")


@client.event
async def on_guild_join(guild):
    client.tree.copy_global_to(guild=guild)
    await client.tree.sync(guild=guild)
    print(f"joined + synced guild {guild.name} ({guild.id})")


client.run(TOKEN)
