
import gradio as gr


# Stubs for Week 6 tabs
def _build_datasets_tab(project: gr.Dropdown, refresh_btn: gr.Button) -> None:
    gr.Markdown("Datasets: coming soon")

def _build_prompts_tab(project: gr.Dropdown, refresh_btn: gr.Button) -> None:
    gr.Markdown("Prompts: coming soon")

def _build_evals_tab(project: gr.Dropdown, refresh_btn: gr.Button) -> None:
    gr.Markdown("Evals: coming soon")

def _build_annotations_tab(project: gr.Dropdown, refresh_btn: gr.Button) -> None:
    gr.Markdown("Annotations: coming soon")

def _build_monitors_tab(project: gr.Dropdown, refresh_btn: gr.Button) -> None:
    gr.Markdown("Monitors: coming soon")

def _build_costs_tab(project: gr.Dropdown, refresh_btn: gr.Button) -> None:
    gr.Markdown("Costs: coming soon")

def _build_settings_tab(project: gr.Dropdown) -> None:
    gr.Markdown("Settings: coming soon")

def _build_ask_hfao_tab() -> None:
    def stub_chat(msg, history):
        return "HFAO Copilot: I am a stub."
    gr.ChatInterface(stub_chat)
