"""Outbound attachment delivery: @@attach upload + marker stripping in the daemon."""

import asyncio
from unittest.mock import AsyncMock, patch

from slack_daemon import SlackDaemon


def _run_deliver(channel, thread_ts, response, *, fake_post, upload_return):
    """Build a SlackDaemon INSIDE the loop, stub its post + upload_files, and
    run _deliver_response. Returns the patched upload_files mock for assertions.

    SlackDaemon.__init__ builds an AsyncSocketModeHandler that creates an
    aiohttp.ClientSession, which requires a running event loop — so the daemon
    MUST be constructed inside asyncio.run, not at module/helper top level.
    self._app.client is a read-only property, so outbound uploads are exercised
    by patching slack_daemon.attachments.upload_files rather than the client.
    """
    async def _main():
        with patch(
            "slack_daemon.attachments.upload_files",
            new=AsyncMock(return_value=upload_return),
        ) as up:
            d = SlackDaemon(bot_token="xoxb-test", app_token="xapp-test")
            d._post_response = fake_post  # type: ignore[assignment]
            await d._deliver_response(channel, thread_ts, response)
            return up

    return asyncio.run(_main())


class TestDeliverResponse:
    def test_uploads_existing_path_strips_marker_and_posts_remainder(self, tmp_path):
        posted = {}

        async def fake_post(channel, thread_ts, text):
            posted["channel"] = channel
            posted["thread_ts"] = thread_ts
            posted["text"] = text

        chart = tmp_path / "chart.png"
        chart.write_bytes(b"PNG")
        reply = f"Here is the chart.\n@@attach {chart}\nDone."

        up = _run_deliver(
            "C1", "T-1", reply, fake_post=fake_post, upload_return=[str(chart)]
        )

        up.assert_awaited_once()
        kwargs = up.await_args.kwargs
        # _deliver_response passes the client positionally and paths as a kwarg.
        assert kwargs["paths"] == [str(chart)]
        assert kwargs["channel"] == "C1"
        assert kwargs["thread_ts"] == "T-1"
        assert posted["text"] == "Here is the chart.\nDone."
        assert "@@attach" not in posted["text"]

    def test_nonexistent_path_still_attempted_but_text_stripped(self, tmp_path):
        # _deliver_response always delegates to upload_files when markers exist;
        # the *existence* skip lives in upload_files itself (covered in Task 3).
        # Here we assert the marker line is stripped from the posted text even
        # when the path is bogus, and upload_files is still called with it.
        posted = {}

        async def fake_post(channel, thread_ts, text):
            posted["text"] = text

        missing = tmp_path / "nope.png"  # never created
        reply = f"text before\n@@attach {missing}\ntext after"

        up = _run_deliver(
            "C1", "T-2", reply, fake_post=fake_post, upload_return=[]
        )

        up.assert_awaited_once()
        assert up.await_args.kwargs["paths"] == [str(missing)]
        assert posted["text"] == "text before\ntext after"

    def test_no_markers_posts_full_text_no_upload(self):
        posted = {}

        async def fake_post(channel, thread_ts, text):
            posted["text"] = text

        up = _run_deliver(
            "C1", "T-3", "just a normal reply", fake_post=fake_post, upload_return=[]
        )

        up.assert_not_awaited()
        assert posted["text"] == "just a normal reply"

    def test_reply_only_marker_posts_nothing(self, tmp_path):
        post_calls = []

        async def fake_post(channel, thread_ts, text):
            post_calls.append(text)

        chart = tmp_path / "only.png"
        chart.write_bytes(b"X")

        up = _run_deliver(
            "C1", "T-4", f"@@attach {chart}", fake_post=fake_post,
            upload_return=[str(chart)],
        )

        up.assert_awaited_once()
        assert post_calls == []  # no text remained, so no chat_postMessage
