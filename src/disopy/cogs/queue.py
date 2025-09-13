# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Holds the cog for queue handling and music playback commands."""

import logging
from collections import deque
from random import sample
from math import ceil
from typing import Iterable, NamedTuple, cast
from itertools import islice

import discord
from discord import PCMVolumeTransformer, VoiceClient, app_commands
import requests
from discord.ext.commands import Bot
from discord.interactions import Interaction
from knuckles import Subsonic

from ..config import Config
from ..options import Options
from .base import Base

class Song(NamedTuple):
    """Data representation for a Subsonic song.

    Attributes:
        id: The ID in the Subsonic server.
        title: The title of the song.
        artist: The primary artist of the song
        duration: The duration of the song in seconds
    """

    id: str
    title: str
    artist: str
    duration: int

logger = logging.getLogger(__name__)


class Queue:
    """Manage the queue and split it per guild."""

    def __init__(self) -> None:
        """Create a new queue."""

        self.queue: dict[str, deque[Song]] = {}

    def _check_guild(self, interaction: Interaction) -> str | None:
        """Check if a guild has an associated queue and if not creates a new one.

        Args:
            interaction: The interaction where the guild ID can be found.

        Returns:
            Either the guild ID or None if the interaction did not have a guild attach to it.
        """

        if interaction.guild is None:
            logger.error("The guild of the interaction was None!")
            return None

        id = str(interaction.guild.id)

        if id not in self.queue:
            self.queue[id] = deque()

        return id

    def get(self, interaction: Interaction) -> Iterable[Song]:
        """Get the queue of a guild.

        Args:
            interaction: The interaction where the guild ID can be found.

        Returns:
            An iterable with the songs of the queue.
        """

        id = self._check_guild(interaction)
        if id is None:
            return []

        return self.queue[id]

    def pop(self, interaction: Interaction) -> Song | None:
        """Remove and get one song from the queue.

        Args:
            interaction: The interaction where the guild ID can be found.

        Returns:
            The next song in the queue or None if the action failed.
        """

        id = self._check_guild(interaction)
        if id is None:
            return None

        return self.queue[id].popleft()

    def append(self, interaction: Interaction, song: Song) -> None:
        """Append new songs to the queue.

        Args:
            interaction: The interaction where the guild ID can be found.
            song: The song to append.
        """

        id = self._check_guild(interaction)
        if id is None:
            return

        return self.queue[id].append(song)

    def length(self, interaction: Interaction) -> int:
        """Get the length of the queue.

        Args:
            interaction: The interaction where the guild ID can be found.

        Returns:
            The length of the queue.
        """

        id = self._check_guild(interaction)
        if id is None:
            # A little ugly but gets the job done
            return 0
 
        return len(self.queue[id])
    
    def shuffle(self, interaction: Interaction) -> None:
        """Shuffles the current queue

        Args:
            interaction: The interaction where the guild ID can be found.

        """
        id = self._check_guild(interaction)
        if id is None:
            return
        self.queue[id] = deque(sample(self.queue[id],len(self.queue[id])))

    def duration(self, interaction: Interaction) -> int:
        """Calculates the remaining duration of the queue, without the current song

        Args:
            interaction: The interaction where the guild ID can be found.

        Returns:
            Seconds of remaining duration
        """
        id = self._check_guild(interaction)
        if id is None:
            return 0
        
        return sum(song.duration for song in self.queue[id])
    
    def clear(self, interaction: Interaction) -> None:
        """Empties the queue
        
        Args:
            interaction: The interaction where the guild ID can be found.
        """
        id = self._check_guild(interaction)
        if id is None:
            return
        
        self.queue[id] = deque()

class QueueCog(Base):
    """Cog that holds queue handling and music playback commands."""

    def __init__(self, bot: Bot, options: Options, subsonic: Subsonic, config: Config) -> None:
        """The constructor of the cog.

        Args:
            bot: The bot attached to the cog.
            options: The options of the program.
            subsonic: The object to be used to access the OpenSubsonic REST API.
            config: The config of the program.
        """

        super().__init__(bot, options)

        self.subsonic = subsonic
        self.config = config

        self.queue = Queue()
        self.now_playing: Song | None = None
        self.loop = 0 # 0: no loop; 1: loop queue; 2: loop track

        self.skip_next_autoplay = False

    async def get_voice_client(self, interaction: Interaction, connect: bool = False) -> VoiceClient | None:
        user = interaction.user
        if isinstance(user, discord.User):
            await self.send_error(interaction, ["You are not a member of the guild, something has gone very wrong..."])
            return None

        if user.voice is None or user.voice.channel is None:
            await self.send_error(interaction, ["You are not connected to any voice channel!"])
            return None

        guild = interaction.guild
        if guild is None:
            await self.send_error(interaction, ["We are not chatting in a guild, something has gone very wrong..."])
            return None

        if guild.voice_client is None:
            if connect:
                self.queue.clear(interaction)
                self.now_playing = None
                self.loop = 0
                return await user.voice.channel.connect(self_deaf=True)
            await self.send_error(interaction, ["I'm not connected to a voice channel!"])
            return None

        if user.voice.channel != guild.voice_client.channel:
            await self.send_error(interaction, ["Join the same voice channel where I am"])
            return None

        return cast(VoiceClient, guild.voice_client)

    def play_next_callback(self, interaction: Interaction, exception: Exception | None) -> None:
        """Callback called when starting the playback of the next song in the queue.

        Args:
            interaction: The interaction where the guild will be extracted.
            exception: An exception that discord.py may have raised.
        """

        if self.skip_next_autoplay:
            self.skip_next_autoplay = False
            return

        self.play_queue(interaction, exception)

    def play_queue(self, interaction: Interaction, exception: Exception | None) -> None:
        """Play the next song in the queue.

        Args:
            interaction: The interaction where the guild will be extracted.
            exception: An exception that discord.py may have raised.
        """

        if exception is not None:
            raise exception

        if self.queue.length(interaction) == 0 and self.loop < 2:
            logger.info("The queue is empty")
            return

        song = self.queue.pop(interaction) if self.loop < 2 else self.now_playing
        if self.loop == 1:
            self.queue.append(interaction, song)

        if song is None:
            logger.error("Unable to get the song for playback")
            return

        logger.info(f"Playing song: '{song.title if song.title else "N/A"}' ({song.id})")

        song_path = self.options.cache_path / "subsonic/songs" / f"{song.id}.audio"

        if not song_path.is_file():
            logger.info("Cache miss, downloading the song...")

            song_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.subsonic.media_retrieval.download(song.id, song_path)

            # Fix to make Disopy work with Funkwhale servers
            except requests.exceptions.HTTPError:
                logger.warning(
                    "Using the /download endpoint for downloading the media failed, using /stream as a fallback"
                )
                self.subsonic.media_retrieval.download(song.id, song_path, use_stream=True)
        else:
            logger.info("Cache hit")

        if interaction.guild is None:
            logger.warning("There is no guild attached to the interaction!")
            return

        if interaction.guild.voice_client is None:
            logger.warning("There is not available voice client in this interaction!")
            return
        
        voice_client = cast(VoiceClient, interaction.guild.voice_client)

        voice_client.play(
            discord.FFmpegPCMAudio(str(song_path.absolute())),
            after=lambda exception: self.play_next_callback(interaction, exception),
        )

        if voice_client.source is None:
            logger.error("The source is not available to attach a volume transformer!")
            return

        self.now_playing = song
        voice_client.source = PCMVolumeTransformer(voice_client.source, volume=self.config.volume / 100)

    async def query_autocomplete(self, interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        """Looks up song/album for autocomplete
        
        Args:
            interaction: The interaction that started the command.
            current: Current input into query.
        """

        results = []

        if len(current) >= 3:
            search = self.subsonic.searching.search(current, song_count=5, album_count=5, artist_count=5)
            if search.songs is not None:
                for song in search.songs:
                    res = f"🎵 {(f"{song.artists[0].name} - " if song.artists[0].name is not None else "")}{song.title}"
                    duration = f" [{self.seconds_to_str(song.duration)}]"
                    # Trunctuate result length if over 100 characters
                    if len(res) + len(duration) > 100:
                        results.append(app_commands.Choice(name=res[:97 - len(duration)] + "..." + duration, value=f"song:{song.id}"))
                    else:
                        results.append(app_commands.Choice(name= res + duration, value=f"song:{song.id}"))
            
            if search.albums is not None:
                for album in search.albums:
                    res = f"🎶 {(f"{album.artists[0].name} - " if album.artists[0] is not None else "")}{album.name}"
                    num_songs = f" ({album.song_count} songs)"
                    # Trunctuate result length if over 100 characters
                    if len(res) + len(num_songs) > 100:
                        results.append(app_commands.Choice(name=res[:97 - len(num_songs)] + "..." + num_songs, value=f"album:{album.id}"))
                    else:
                        results.append(app_commands.Choice(name=res + num_songs, value=f"album:{album.id}"))

            if len(results) == 0:
                results = [app_commands.Choice(name="No result found :(", value="")]        
        else:
            results = [app_commands.Choice(name="Input 3 or more letters to search", value="")]
        return results
    
    @app_commands.command(description="Add a song or album to the queue")
    @app_commands.autocomplete(query=query_autocomplete)
    async def play(
        self,
        interaction: Interaction,
        query: str,
    ) -> None:
        """Add a song in the queue and start the playback if it's stop.

        Args:
            interaction: The interaction that started the command.
            query: The type of media to play and its id.
        """

        
        voice_client = await self.get_voice_client(interaction, True)
        if voice_client is None:
            return

        choice, value = query.split(":")
        first_play = self.queue.length(interaction) == 0 and self.now_playing is None
        playing_element_name = ""
        songs_added = 0

        match choice:
            case "song":
                song = self.subsonic.browsing.get_song(value)
                if song is None:
                    await self.send_error(interaction, [f"No song found"])
                    return
                
                if song.title is None:
                    await self.send_error(interaction, [f"The song is missing the required metadata: {query}"])
                    return
                
                playing_element_name = song.title
                self.queue.append(interaction, Song(song.id, song.title, song.artists[0].name, song.duration))

            case "album":
                album = self.subsonic.browsing.get_album(value)
                if album is None:
                    await self.send_error(interaction, [f"No album found"])
                    return
                
                if album.songs is None:
                    await self.send_error(interaction, [f"The album is missing the required metadata: {query}"])
                    return
                
                if album.name is not None:
                    playing_element_name = album.name

                for song in album.songs:
                    if song.title is None:
                        logger.error(f"The song with ID '{song.id}' is missing the name metadata entry")
                        continue
                    songs_added += 1
                    self.queue.append(interaction, Song(song.id, song.title, song.artists[0].name, song.duration))

            case _:
                await self.send_error(interaction, [f"No songs found"])
                return

        if first_play:
            await self.send_answer(interaction, "🎵 Now playing!", [f"**{playing_element_name}**{f" ({songs_added} songs added)" if songs_added > 0 else ""}"])
        else:
            await self.send_answer(interaction, "🎧 Added to the queue", [f"**{playing_element_name}**{f" ({songs_added} songs added)" if songs_added > 0 else ""}"])

        if not voice_client.is_playing():
            self.play_queue(interaction, None)

    @app_commands.command(description="Adds a playlist to the queue")
    async def playlist(self, interaction: Interaction, query: str) -> None:
        """Queues a playlist
        
        Args:
            interaction: The interaction that started the command.
            query: The name of the playlist
        """
        voice_client = await self.get_voice_client(interaction, True)
        if voice_client is None:
            return

        first_play = self.queue.length(interaction) == 0 and self.now_playing is None
        playing_element_name = query
        songs_added = 0

        for playlist in self.subsonic.playlists.get_playlists():
            if playlist.name is None:
                continue
            if query.lower() in playlist.name.lower():
                playlist = playlist.generate()
                if playlist.songs is None:
                    await self.send_error(interaction, ["The playlist has no songs!"])
                    return
                if playlist.name is not None:
                    playing_element_name = playlist.name
                for song in playlist.songs:
                    if song.title is None:
                        logger.error(f"The song with ID '{song.id}' is missing the name metadata entry")
                        continue
                    songs_added += 1
                    self.queue.append(interaction, Song(song.id, song.title, song.artists[0].name, song.duration))
                break

        if first_play:
            await self.send_answer(interaction, "🎵 Now playing!", [f"**{playing_element_name}** ({songs_added} songs added)"])
        else:
            await self.send_answer(interaction, "🎧 Added to the queue", [f"**{playing_element_name}** ({songs_added} songs added)"])

        if not voice_client.is_playing():
            self.play_queue(interaction, None)

    @app_commands.command(description="Stop the current song")
    async def stop(self, interaction: Interaction) -> None:
        """Stop the song that is currently playing.

        Args:
            interaction: The interaction that started the command.
        """

        voice_client = await self.get_voice_client(interaction)
        if voice_client is None:
            return

        if not voice_client.is_playing():
            await self.send_error(interaction, ["No song currently playing!"])
            return

        self.skip_next_autoplay = True
        voice_client.stop()
        self.now_playing = None

        await self.send_answer(interaction, "🛑 Song stopped")

    @app_commands.command(description="Pause the current song")
    async def pause(self, interaction: Interaction) -> None:
        """Pause the song that is currently playing.

        Args:
            interaction: The interaction that started the command.
        """

        voice_client = await self.get_voice_client(interaction)
        if voice_client is None:
            return

        if not voice_client.is_playing():
            await self.send_error(interaction, ["No song currently playing!"])
            return

        voice_client.pause()
        await self.send_answer(interaction, "⏸️ Song paused")

    @app_commands.command(description="Skip the current song")
    async def skip(self, interaction: Interaction) -> None:
        """Skip the currently playing song.

        Args:
            interaction: The interaction that started the command.
        """

        voice_client = await self.get_voice_client(interaction)
        if voice_client is None:
            return

        if not voice_client.is_playing():
            await self.send_error(interaction, ["No song currently playing!"])
            return

        voice_client.stop()
        await self.send_answer(interaction, "⏭️ Song skipped")

    @app_commands.command(description="Clear the queue")
    async def clear(self, interaction: Interaction) -> None:
        """Clears the remaining queue
        
        Args:
            interaction: The interaction that started the command.
        """
        voice_client = await self.get_voice_client(interaction)
        if voice_client is None:
            return

        self.queue.clear(interaction)
        await self.send_answer(interaction, "🗑️ Cleared the queue")

    @app_commands.command(description="Resume the playback")
    async def resume(self, interaction: Interaction) -> None:
        """Resume the playback of the song and if there is no one playing play the next one in the queue.

        Args:
            interaction: The interaction that started the command.
        """

        voice_client = await self.get_voice_client(interaction)
        if voice_client is None:
            return

        if voice_client.is_paused():
            voice_client.resume()
            await self.send_answer(interaction, "▶️ Resuming the song")
            return

        if self.queue.length(interaction) == 0:
            await self.send_error(interaction, ["The queue is empty"])
            return

        self.play_queue(interaction, None)
        await self.send_answer(interaction, "▶️ Resuming the playback")

    @app_commands.command(description="Kick the bot from the voice call")
    async def leave(self, interaction: Interaction) -> None:
        user = interaction.user
        if isinstance(user, discord.User):
            await self.send_error(interaction, ["You are not a member of the guild, something has gone very wrong..."])
            return None

        if user.voice is None or user.voice.channel is None:
            await self.send_error(interaction, ["You are not connected to any voice channel!"])
            return None

        guild = interaction.guild
        if guild is None:
            await self.send_error(interaction, ["We are not chatting in a guild, something has gone very wrong..."])
            return None

        if guild.voice_client is None:
            await self.send_error(interaction, ["I'm not connected to a voice channel!"])
            return None

        if user.voice.channel != guild.voice_client.channel:
            await self.send_error(interaction, ["Join the same voice channel where I am"])
            return None
        try:
            await guild.voice_client.disconnect()
        finally:
            await self.send_answer(interaction, "🚪 Bot left", ["Goodbye."])

    @app_commands.command(name="loop", description="Loops the queue")
    @app_commands.choices(
        what = [
            app_commands.Choice(name="Queue", value=1),
            app_commands.Choice(name="Song", value=2)
        ]
    )
    async def loop_command(self, interaction: Interaction, what: app_commands.Choice[int] = 1) -> None:
        """Loops the queue / current track
        
        Args:
            interaction: The interaction that started the command.
            what: How to loop queue
        """
        voice_client = await self.get_voice_client(interaction)
        if voice_client is None:
            return
        
        if self.queue.length(interaction) == 0 and what.value != 2:
            await self.send_error(interaction, ["The queue is empty"])
            return
        if self.now_playing is None and what.value == 2:
            await self.send_error(interaction, ["Nothing is playing"])
            return

        if what.value == self.loop:
            self.loop = 0
            await self.send_answer(interaction, "🔁 Stopped looping")
            return

        if what.value == 1 and self.now_playing is not None and self.now_playing != self.queue[-1]:
            self.queue.append(interaction, self.now_playing)

        self.loop = what.value
        await self.send_answer(interaction, "🔁 Now looping queue" if what == 1 else "🔂 Now looping current track")

    @app_commands.command(name="shuffle", description="Shuffles the current queue")
    async def shuffle_command(self, interaction: Interaction) -> None:
        """Mixes the songs in the queue, if one exists

        Args:
            interaction: The interaction that started the command.
        """
        voice_client = await self.get_voice_client(interaction)
        if voice_client is None:
            return
        
        if self.queue.length(interaction) == 0:
            await self.send_error(interaction, ["The queue is empty"])
            return
        
        self.queue.shuffle(interaction)
        await self.send_answer(interaction, "🔀 Shuffling the queue")

    @app_commands.command(name="queue", description="See the current queue")
    # Name changed to avoid collisions with the property `queue`
    async def queue_command(self, interaction: Interaction, page: int = 1) -> None:
        """List the songs added to the queue.

        Args:
            interaction: The interaction that started the command.
        """
        content = []
        max_page = ceil(self.queue.length(interaction)/10)
        length = self.queue.length(interaction)

        if (1 > page or page > max_page) and max_page != 0:
            await self.send_error(interaction, ["Out of queue bounds"])

        if self.now_playing is not None:
            content.append(f"Now playing: {self.now_playing.artist} - **{self.now_playing.title}**")
            content.append("")

        page -= 1
        if self.loop > 0:
            content.append(f"Looping {"queue" if self.loop == 1 else "track"}")
        if length > 0:
            content.append(f"""Remaining time - {self.seconds_to_str(self.queue.duration(interaction))}
                           Pages - {page+1}/{max_page}
                           
                           Next:""")
            for num, song in enumerate(islice(self.queue.get(interaction), 10*page, 10*(page + 1))):
                content.append(f"{10*page + num + 1}. {song.artist} - **{song.title}**\t[{self.seconds_to_str(song.duration)}]")

        if length == 0:
            content.append("_Queue empty_")

        await self.send_answer(interaction, f"🎹 Queue ({length} songs remaining)", content)

    @app_commands.command(description="Adjust the volume")
    async def volume(self, interaction: Interaction, volume: int) -> None:
        """Adjust the volume of the playback.

        Args:
            interaction: The interaction that started the command.
            volume: The new volume level.
        """

        # Defer immediately to avoid timeout
        # await interaction.response.defer(thinking=True)

        voice_client = await self.get_voice_client(interaction)
        if voice_client is None:
            return

        if volume < 0:
            await self.send_error(interaction, ["The requested volume must be at least 0%"])
            return

        if voice_client.source is None:
            await self.send_error(interaction, ["The voice client source is not available"])
            return

        # Every source has a volume handler attach to it so suppressing the mypy error is safe
        voice_client.source.volume = volume / 100  # type: ignore[attr-defined]
        await self.send_answer(interaction, f"🔊 Volume level set to {volume}%")