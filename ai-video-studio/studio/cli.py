import json
import typer
from rich.console import Console
from . import core

app = typer.Typer(help="AI Video Studio v0.5 — storyboard and video generation")
console = Console()

@app.command()
def create(prompt: str = typer.Option(..., help="Video concept"),
           duration: int = typer.Option(60, help="Total duration in seconds"),
           provider: str = typer.Option("mock", help="mock, minimax, wan, flux"),
           budget: float = typer.Option(0.0, help="USD spending ceiling")):
    try:
        pid = core.create(prompt, duration, provider, budget)
        console.print(f"Project created: [bold]{pid}[/bold]")
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]{e}[/red]"); raise typer.Exit(1)

@app.command()
def plan(project_id: str, shot_length: int = typer.Option(10, help="Seconds per shot")):
    try:
        console.print(f"Planned {core.plan(project_id, shot_length)} shots")
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]{e}[/red]"); raise typer.Exit(1)

@app.command()
def generate(project_id: str, live: bool = typer.Option(False, help="Explicitly allow paid API calls"),
             budget: float = typer.Option(None, help="Optional additional USD ceiling"),
             limit: int = typer.Option(0, help="Max shots this run"),
             poll_interval: int = typer.Option(15, help="Poll interval in seconds")):
    try:
        console.print(core.generate(project_id, live=live, budget=budget, limit=limit, poll_interval=poll_interval))
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]{e}[/red]"); raise typer.Exit(1)

@app.command()
def render(project_id: str):
    try:
        console.print(f"Rendered: [green]{core.render(project_id)}[/green]")
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]{e}[/red]"); raise typer.Exit(1)

@app.command()
def status(project_id: str):
    try:
        console.print_json(json.dumps(core.status(project_id), ensure_ascii=False))
    except ValueError as e:
        console.print(f"[red]{e}[/red]"); raise typer.Exit(1)


@app.command()
def attach(project_id: str, shot_id: str, remote_id: str, confirm: bool = typer.Option(False, help="Confirm remote task ID was checked in provider dashboard")):
    if not confirm:
        console.print("[red]Requires --confirm after checking provider dashboard[/red]")
        raise typer.Exit(1)
    try:
        core.attach(project_id,shot_id,remote_id)
        console.print("Attached remote task")
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


@app.command("smart-plan")
def smart_plan_cmd(project_id: str, model: str = typer.Option(...), shot_length: int = typer.Option(10),
                   base_url: str = typer.Option(None)):
    try:
        console.print(f"Planned {core.smart_plan(project_id,shot_length,model,base_url)} LLM-directed shots")
    except (ValueError, RuntimeError, Exception) as e:
        console.print(f"[red]{e}[/red]"); raise typer.Exit(1)

@app.command("storyboard")
def storyboard_cmd(project_id: str, output: str = typer.Option(None)):
    try:
        if output:
            console.print(f"Saved: {core.save_storyboard(project_id,output)}")
        else:
            console.print_json(json.dumps(core.storyboard(project_id),ensure_ascii=False))
    except ValueError as e:
        console.print(f"[red]{e}[/red]"); raise typer.Exit(1)

@app.command("edit-shot")
def edit_shot_cmd(project_id: str, seq: int, prompt: str = typer.Option(...)):
    try:
        console.print(f"Updated: {core.update_shot(project_id,seq,prompt)}")
    except ValueError as e:
        console.print(f"[red]{e}[/red]"); raise typer.Exit(1)



@app.command("characters")
def characters_cmd(project_id: str):
    from . import references
    console.print_json(json.dumps(references.characters(project_id), ensure_ascii=False))

@app.command("import-character")
def import_character_cmd(project_id: str, name: str, image: str):
    from . import references
    console.print_json(json.dumps(references.import_character(project_id, name, image)))

@app.command("import-first-frame")
def import_first_frame_cmd(project_id: str, seq: int, image: str):
    from . import references
    console.print_json(json.dumps(references.import_shot(project_id, seq, image)))

@app.command("preview-frames")
def preview_frames_cmd(project_id: str):
    from . import references
    console.print_json(json.dumps(references.shot_reference_plan(project_id), ensure_ascii=False))

@app.command("extract-keyframes")
def extract_keyframes_cmd(project_id: str, seq: int, count: int = 3):
    from . import references
    console.print_json(json.dumps(references.extract_keyframes(project_id, seq, count)))

@app.command("check-i2v")
def check_i2v_cmd(project_id: str, seq: int):
    from . import references
    console.print_json(json.dumps(references.validate_i2v(project_id, seq)))

if __name__ == "__main__":
    app()

@app.command("frame-plan")
def frame_plan_cmd(project_id: str, seq: int):
    from . import reference_generation as rg
    console.print_json(json.dumps(rg.frame_plan(project_id,seq),ensure_ascii=False))

@app.command("generate-frame")
def generate_frame_cmd(project_id: str, seq: int, live: bool = False,
                       model: str = "gpt-image-2.5-sunburst", force: bool = False):
    from . import reference_generation as rg
    try: console.print_json(json.dumps(rg.generate_frame(project_id,seq,live=live,model=model,force=force),ensure_ascii=False))
    except (ValueError,RuntimeError) as e: console.print(f"[red]{e}[/red]");raise typer.Exit(1)

@app.command("generate-i2v")
def generate_i2v_cmd(project_id: str, seq: int, live: bool = False,
                     max_usd: float = typer.Option(None,help="Mandatory paid-video ceiling")):
    from . import reference_generation as rg
    try: console.print_json(json.dumps(rg.i2v(project_id,seq,live=live,max_usd=max_usd),ensure_ascii=False))
    except (ValueError,RuntimeError) as e: console.print(f"[red]{e}[/red]");raise typer.Exit(1)

@app.command("poll-i2v")
def poll_i2v_cmd(project_id: str, seq: int):
    from . import reference_generation as rg
    try: console.print_json(json.dumps(rg.poll_i2v(project_id,seq),ensure_ascii=False))
    except (ValueError,RuntimeError) as e: console.print(f"[red]{e}[/red]");raise typer.Exit(1)


@app.command("sync-assets")
def sync_assets_cmd(project_id: str):
    from . import timeline
    console.print_json(json.dumps(timeline.sync(project_id)))

@app.command("assets")
def assets_cmd(project_id: str, shot: int = typer.Option(None)):
    from . import timeline
    console.print_json(json.dumps(timeline.assets(project_id,shot)))

@app.command("import-asset")
def import_asset_cmd(project_id: str, seq: int, file: str, source: str = "manual"):
    from . import timeline
    console.print(timeline.import_asset(project_id,seq,file,source))

@app.command("select-asset")
def select_asset_cmd(project_id: str, seq: int, asset: str = typer.Option(...)):
    from . import timeline
    console.print(timeline.select(project_id,seq,asset))

@app.command("timeline")
def timeline_cmd(project_id: str):
    from . import timeline
    console.print_json(json.dumps(timeline.timeline(project_id)))

@app.command("render-timeline")
def render_timeline_cmd(project_id: str, source: str = "selected", output: str = None):
    from . import timeline
    try:
        console.print(f"Rendered: {timeline.render(project_id,output,source)}")
    except (ValueError,RuntimeError) as e:
        console.print(f"[red]{e}[/red]"); raise typer.Exit(1)
