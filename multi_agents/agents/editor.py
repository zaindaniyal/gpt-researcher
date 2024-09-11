from datetime import datetime
from .utils.views import print_agent_output
from .utils.llms import call_model
from langgraph.graph import StateGraph, END
import asyncio
import json

from ..memory.draft import DraftState
from . import ResearchAgent, ReviewerAgent, ReviserAgent


class EditorAgent:
    def __init__(self, websocket=None, stream_output=None, headers=None):
        self.websocket = websocket
        self.stream_output = stream_output
        self.headers = headers or {}

    async def plan_research(self, research_state: dict):
        """
        Curate relevant sources for a query
        :param summary_report:
        :return:
        :param total_sub_headers:
        :return:
        """

        initial_research = research_state.get("initial_research")
        task = research_state.get("task")
        include_human_feedback = task.get("include_human_feedback")
        human_feedback = research_state.get("human_feedback")
        max_sections = task.get("max_sections")

        prompt = [
            {
                "role": "system",
                "content": "You are a research editor. Your goal is to oversee the research project"
                " from inception to completion. Your main task is to plan the article section "
                "layout based on an initial research summary.\n ",
            },
            {
                "role": "user",
                "content": f"""Today's date is {datetime.now().strftime('%d/%m/%Y')}
                                  Research summary report: '{initial_research}'
                                  {f'Human feedback: {human_feedback}. You must plan the sections based on the human feedback.'
            if include_human_feedback and human_feedback and human_feedback != 'no' else ''}
                                  \nYour task is to generate an outline of sections headers for the research project
                                  based on the research summary report above.
                                  You must generate a maximum of {max_sections} section headers.
                                  You must focus ONLY on related research topics for subheaders and do NOT include introduction, conclusion and references.
                                  You must return nothing but a JSON with the fields 'title' (str) and 
                                  'sections' (maximum {max_sections} section headers) with the following structure:
                                  '{{title: string research title, date: today's date, 
                                  sections: ['section header 1', 'section header 2', 'section header 3' ...]}}.""",
            },
        ]

        print_agent_output(
            f"Planning an outline layout based on initial research...", agent="EDITOR"
        )
        plan = await call_model(
            prompt=prompt,
            model=task.get("model"),
            response_format="json",
        )

        return {
            "title": plan.get("title"),
            "date": plan.get("date"),
            "sections": plan.get("sections"),
        }

    async def run_parallel_research(self, research_state: dict):
        research_agent = ResearchAgent(self.websocket, self.stream_output, self.headers)
        reviewer_agent = ReviewerAgent(self.websocket, self.stream_output, self.headers)
        reviser_agent = ReviserAgent(self.websocket, self.stream_output, self.headers)
        queries = research_state.get("sections")
        title = research_state.get("title")
        human_feedback = research_state.get("human_feedback")
        workflow = StateGraph(DraftState)

        workflow.add_node("researcher", research_agent.run_depth_research)
        workflow.add_node("reviewer", reviewer_agent.run)
        workflow.add_node("reviser", reviser_agent.run)

        # set up edges researcher->reviewer->reviser->reviewer...
        workflow.set_entry_point("researcher")
        workflow.add_edge("researcher", "reviewer")
        workflow.add_edge("reviser", "reviewer")
        workflow.add_conditional_edges(
            "reviewer",
            (lambda draft: "accept" if draft["review"] is None else "revise"),
            {"accept": END, "revise": "reviser"},
        )

        chain = workflow.compile()

        # Execute the graph for each query in parallel
        if self.websocket and self.stream_output:
            await self.stream_output(
                "logs",
                "parallel_research",
                f"Running parallel research for the following queries: {queries}",
                self.websocket,
            )
        else:
            print_agent_output(
                f"Running the following research tasks in parallel: {queries}...",
                agent="EDITOR",
            )

        final_drafts = [
            chain.ainvoke(
                {
                    "task": research_state.get("task"),
                    "topic": query,  # + (f". Also: {human_feedback}" if human_feedback is not None else ""),
                    "title": title,
                    "headers": self.headers,
                }
            )
            for query in queries
        ]
        research_results = [
            result["draft"] for result in await asyncio.gather(*final_drafts)
        ]

        return {"research_data": research_results}
