from typing import List, Set, Optional, Dict, TYPE_CHECKING

from llama_cpp import Llama
from llama_index.core import VectorStoreIndex
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.core.schema import NodeWithScore
from llama_index.core import VectorStoreIndex, SimpleDirectoryReader, Settings
from llama_index.core.workflow import Context
from llama_index.core.postprocessor import SimilarityPostprocessor

from llama_index.core.workflow import Event, StartEvent, StopEvent, Workflow, step


def convert_message_list_to_str(messages):
    chat_history_str = ""
    for message in messages:
        if type(message["content"]) is str:
            chat_history_str += message["role"] + ": " + message["content"] + "\n"
        else:
            chat_history_str += message["role"] + ": " + message["content"]["text"] + "\n"

    return chat_history_str


class SetupEvent(Event):
    pass


class CondenseQueryEvent(Event):
    condensed_query_str: str


class RetrievalEvent(Event):
    nodes: List[NodeWithScore]


class ChatHistory:
    def __init__(self, max_history_size: int):
        self.max_history_size = max_history_size
        self.total_size = 0
        self.chat_history = []

    def add_message(self, message: dict):
        if "content" in message and type(message["content"]) is list:
            # multimodal model's message format
            new_msg_size = len(message["content"][0]["text"].split())
        else:
            # text-only model's message format
            new_msg_size = len(message["content"].split())

        self.total_size += new_msg_size
        self.chat_history.append(message)
        while self.total_size > self.max_history_size:
            oldest_msg = self.chat_history.pop(0)
            if "content" in oldest_msg and type(oldest_msg["content"]) is list:
                # multimodal model's message format
                len_oldest_msg = len(oldest_msg["content"][0]["text"].split())
            else:
                # text-only model's message format
                len_oldest_msg = len(oldest_msg["content"].split())
            self.total_size -= len_oldest_msg

    def get_chat_history(self):
        return self.chat_history

    def clear(self):
        self.chat_history = []
        self.total_size = 0

    def __len__(self):
        return len(self.chat_history)


class RAGAgent(Workflow):
    SUMMARY_TEMPLATE = (
        "Given the converstation:\n"
        "'''{chat_history_str}'''\n\n"
        "Rewrite to a standalone question:\n"
    )

    CONTEXT_PROMPT_TEMPLATE = (
        "Information that might help:\n"
        "-----\n"
        "{node_context}\n"
        "-----\n"
        "Please write a response to the following question, using the above information if relevant:\n"
        "{query_str}\n"
    )
    SYSTEM_PROMPT = {"role": "system", "content": "Generate short and simple response."}

    def __init__(
        self,
        generation_setting: Dict,
        rag_setting: Dict,
        llm: Llama,
        timeout: int = 60,
        verbose: bool = False,
    ):
        super().__init__(timeout=timeout, verbose=verbose)
        self.generation_setting = generation_setting
        self.context_len = generation_setting["context_len"]
        # we want the retrieved context = our set value but not more than the context_len
        self.retrieval_top_k = (self.context_len - rag_setting["chunk_size"]) // rag_setting[
            "chunk_overlap"
        ]
        self.retrieval_top_k = min(max(1, self.retrieval_top_k), rag_setting["max_retrieval_top_k"])
        self.search_index = None
        self.retriever = None
        # 1 token ~ 3/4 words, because the context length accounts for both input and output tokens
        # we need to reserve some space for the output tokens (half-space for output tokens)
        self.chat_history = ChatHistory(max_history_size=self.context_len * 0.75 * 1 / 2)
        self.lookup_files = set()

        self.embed_model = HuggingFaceEmbedding(model_name=rag_setting["embed_model_name"])
        Settings.embed_model = self.embed_model
        Settings.chunk_size = rag_setting["chunk_size"]
        Settings.chunk_overlap = rag_setting["chunk_overlap"]
        self.node_processor = SimilarityPostprocessor(
            similarity_cutoff=rag_setting["similarity_threshold"]
        )
        self.llm = llm

    def update_index(self, files: Optional[Set[str]] = set()):
        if not files:
            print("No lookup files provided, clearing index...")
            self.retriever = None
            self.search_index = None
            return

        print("Indexing documents...")
        documents = SimpleDirectoryReader(input_files=files, recursive=True).load_data(
            show_progress=True, num_workers=1
        )

        if self.search_index is None:
            self.search_index = VectorStoreIndex.from_documents(
                documents, embed_model=self.embed_model
            )
        else:
            for doc in documents:
                self.search_index.insert(doc)  # Add the new document to the index

        self.retriever = self.search_index.as_retriever(similarity_top_k=self.retrieval_top_k)

    def update_rag_setting(self, rag_setting: Dict):
        if self.node_processor.similarity_cutoff != rag_setting["similarity_threshold"]:
            self.node_processor = SimilarityPostprocessor(
                similarity_cutoff=rag_setting["similarity_threshold"]
            )

        new_top_k = (self.context_len - rag_setting["chunk_size"]) // rag_setting["chunk_overlap"]
        new_top_k = min(max(1, new_top_k), rag_setting["max_retrieval_top_k"])
        if self.retrieval_top_k != new_top_k:
            self.retrieval_top_k = new_top_k
            if self.retriever:
                self.retriever = self.search_index.as_retriever(
                    similarity_top_k=self.retrieval_top_k
                )

        if (
            self.embed_model.model_name != rag_setting["embed_model_name"]
            or Settings.chunk_size != rag_setting["chunk_size"]
            or Settings.chunk_overlap != rag_setting["chunk_overlap"]
        ):
            self.embed_model = HuggingFaceEmbedding(model_name=rag_setting["embed_model_name"])
            Settings.embed_model = self.embed_model
            Settings.chunk_size = rag_setting["chunk_size"]
            Settings.chunk_overlap = rag_setting["chunk_overlap"]

            # reindex since those are the settings that affect the index
            if self.lookup_files:
                print("Re-indexing documents since the rag settings have changed...")
                documents = SimpleDirectoryReader(
                    input_files=self.lookup_files, recursive=True
                ).load_data(show_progress=True, num_workers=1)
                self.search_index = VectorStoreIndex.from_documents(
                    documents, embed_model=self.embed_model
                )
                self.retriever = self.search_index.as_retriever(
                    similarity_top_k=self.retrieval_top_k
                )

    def update_generation_setting(self, generation_setting):
        self.generation_setting = generation_setting
        self.context_len = generation_setting["context_len"]

    @step
    async def setup(self, ctx: Context, ev: StartEvent) -> SetupEvent:
        # set frequently used variables to context
        query_str = ev.query_str
        image = ev.image
        lookup_files = ev.lookup_files if ev.lookup_files else set()
        streaming = ev.streaming
        await ctx.set("query_str", query_str)
        await ctx.set("image", image)
        await ctx.set("streaming", streaming)

        # update index if needed
        if lookup_files != self.lookup_files:
            print("Different lookup files, updating index...")
            self.update_index(lookup_files)

        self.lookup_files = lookup_files.copy()

        return SetupEvent()

    @step
    async def condense_history_to_query(self, ctx: Context, ev: SetupEvent) -> CondenseQueryEvent:
        """
        Condense the chat history and the query into a single query. Only used for retrieval.
        """
        query_str = await ctx.get("query_str")

        formated_query = ""

        if len(self.chat_history) > 0 or self.retriever is not None:
            self.chat_history.add_message({"role": "user", "content": query_str})
            chat_history_str = convert_message_list_to_str(self.chat_history.get_chat_history())
            # use llm to summarize the chat history into a single query,
            # which is used to retrieve context from the documents
            formated_query = self.SUMMARY_TEMPLATE.format(chat_history_str=chat_history_str)

            history_summary = self.llm.create_chat_completion(
                messages=[{"role": "user", "content": formated_query}],
                stream=False,
                top_k=self.generation_setting["top_k"],
                top_p=self.generation_setting["top_p"],
                temperature=self.generation_setting["temperature"],
            )["choices"][0]["message"]["content"]

            condensed_query = "Context:\n" + history_summary + "\nQuestion: " + query_str
            # remove the last user message from the chat history
            # later we will add the query with retrieved context (RAG)
            self.chat_history.chat_history.pop()
        else:
            # if there is no history or no need for retrieval, return the query as is
            condensed_query = query_str

        return CondenseQueryEvent(condensed_query_str=condensed_query)

    @step
    async def retrieve(self, ctx: Context, ev: CondenseQueryEvent) -> RetrievalEvent:
        if not self.retriever:
            return RetrievalEvent(nodes=[])

        # retrieve from dropped documents
        condensed_query_str = ev.condensed_query_str
        nodes = await self.retriever.aretrieve(condensed_query_str)
        nodes = self.node_processor.postprocess_nodes(nodes)
        return RetrievalEvent(nodes=nodes)

    def _prepare_query_with_context(
        self,
        query_str: str,
        nodes: List[NodeWithScore],
    ) -> str:
        node_context = ""

        if len(nodes) == 0:
            return query_str

        for idx, node in enumerate(nodes):
            node_text = node.get_content(metadata_mode="llm")
            node_context += f"\n{node_text}\n\n"

        formatted_query = self.CONTEXT_PROMPT_TEMPLATE.format(
            node_context=node_context, query_str=query_str
        )

        return formatted_query

    @step
    async def llm_response(self, ctx: Context, retrieval_ev: RetrievalEvent) -> StopEvent:
        nodes = retrieval_ev.nodes
        query_str = await ctx.get("query_str")
        image = await ctx.get("image")
        query_with_ctx = self._prepare_query_with_context(query_str, nodes)
        streaming = await ctx.get("streaming", False)

        if image:
            formated_message = {
                "role": "user",
                "content": [
                    {"type": "text", "text": query_with_ctx},
                    {"type": "image_url", "image_url": {"url": image}},
                ],
            }
        else:
            formated_message = {"role": "user", "content": query_with_ctx}

        self.chat_history.add_message(formated_message)

        response = self.llm.create_chat_completion(
            messages=[self.SYSTEM_PROMPT] + self.chat_history.get_chat_history(),
            stream=streaming,
            top_k=self.generation_setting["top_k"],
            top_p=self.generation_setting["top_p"],
            temperature=self.generation_setting["temperature"],
        )
        # remove the query with context from the chat history,
        self.chat_history.chat_history.pop()
        # add the short query (without context) instead -> not to clutter the chat history
        self.chat_history.add_message({"role": "user", "content": query_str})

        return StopEvent(result=response)