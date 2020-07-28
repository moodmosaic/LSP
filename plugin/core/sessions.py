from .collections import DottedDict
from .edit import parse_workspace_edit
from .logging import debug
from .logging import exception_log
from .protocol import TextDocumentSyncKindNone, TextDocumentSyncKindIncremental, CompletionItemTag
from .protocol import WorkspaceFolder, Request, Notification, Response
from .rpc import Client
from .rpc import Logger
from .settings import client_configs
from .transports import Transport
from .types import ClientConfig
from .types import ClientStates
from .types import debounced
from .types import diff
from .typing import Callable, Dict, Any, Optional, List, Tuple, Generator, Type, Protocol
from .url import uri_to_filename
from .version import __version__
from .views import COMPLETION_KINDS
from .views import did_change_configuration
from .views import extract_variables
from .views import SYMBOL_KINDS
from .workspace import is_subpath_of
from abc import ABCMeta, abstractmethod
from weakref import WeakSet
import os
import sublime
import weakref


InitCallback = Callable[['Session', bool], None]


class Manager(metaclass=ABCMeta):
    """
    A Manager is a container of Sessions.
    """

    # Observers

    @abstractmethod
    def window(self) -> sublime.Window:
        """
        Get the window associated with this manager.
        """
        pass

    @abstractmethod
    def sessions(self, view: sublime.View, capability: Optional[str] = None) -> 'Generator[Session, None, None]':
        """
        Iterate over the sessions stored in this manager, applicable to the given view, with the given capability.
        """
        pass

    @abstractmethod
    def get_project_path(self, file_path: str) -> Optional[str]:
        """
        Get the project path for the given file.
        """
        pass

    # Mutators

    @abstractmethod
    def start_async(self, configuration: ClientConfig, initiating_view: sublime.View) -> None:
        """
        Start a new Session with the given configuration. The initiating view is the view that caused this method to
        be called.

        A normal flow of calls would be start -> on_post_initialize -> do language server things -> on_post_exit.
        However, it is possible that the subprocess cannot start, in which case on_post_initialize will never be called.
        """
        pass

    @abstractmethod
    def update_diagnostics_panel_async(self) -> None:
        pass

    @abstractmethod
    def show_diagnostics_panel_async(self) -> None:
        pass

    @abstractmethod
    def hide_diagnostics_panel_async(self) -> None:
        pass

    # Event callbacks

    @abstractmethod
    def on_post_exit_async(self, session: 'Session', exit_code: int, exception: Optional[Exception]) -> None:
        """
        The given Session has stopped with the given exit code.
        """
        pass


def get_initialize_params(variables: Dict[str, str], workspace_folders: List[WorkspaceFolder],
                          config: ClientConfig) -> dict:
    completion_kinds = list(range(1, len(COMPLETION_KINDS) + 1))
    symbol_kinds = list(range(1, len(SYMBOL_KINDS) + 1))
    completion_tag_value_set = [v for k, v in CompletionItemTag.__dict__.items() if not k.startswith('_')]
    first_folder = workspace_folders[0] if workspace_folders else None
    capabilities = {
        "textDocument": {
            "synchronization": {
                "dynamicRegistration": True,  # exceptional
                "didSave": True,
                "willSave": True,
                "willSaveWaitUntil": True
            },
            "hover": {
                "dynamicRegistration": True,
                "contentFormat": ["markdown", "plaintext"]
            },
            "completion": {
                "dynamicRegistration": True,
                "completionItem": {
                    "snippetSupport": True,
                    "deprecatedSupport": True,
                    "documentationFormat": ["markdown", "plaintext"],
                    "tagSupport": {
                        "valueSet": completion_tag_value_set
                    }
                },
                "completionItemKind": {
                    "valueSet": completion_kinds
                }
            },
            "signatureHelp": {
                "dynamicRegistration": True,
                "signatureInformation": {
                    "documentationFormat": ["markdown", "plaintext"],
                    "parameterInformation": {
                        "labelOffsetSupport": True
                    }
                }
            },
            "references": {
                "dynamicRegistration": True
            },
            "documentHighlight": {
                "dynamicRegistration": True
            },
            "documentSymbol": {
                "dynamicRegistration": True,
                "hierarchicalDocumentSymbolSupport": True,
                "symbolKind": {
                    "valueSet": symbol_kinds
                }
            },
            "formatting": {
                "dynamicRegistration": True  # exceptional
            },
            "rangeFormatting": {
                "dynamicRegistration": True
            },
            "declaration": {
                "dynamicRegistration": True,
                "linkSupport": True
            },
            "definition": {
                "dynamicRegistration": True,
                "linkSupport": True
            },
            "typeDefinition": {
                "dynamicRegistration": True,
                "linkSupport": True
            },
            "implementation": {
                "dynamicRegistration": True,
                "linkSupport": True
            },
            "codeAction": {
                "dynamicRegistration": True,
                "codeActionLiteralSupport": {
                    "codeActionKind": {
                        "valueSet": [
                            "quickfix",
                            "refactor",
                            "refactor.extract",
                            "refactor.inline",
                            "refactor.rewrite",
                            "source.organizeImports"
                        ]
                    }
                }
            },
            "rename": {
                "dynamicRegistration": True,
                "prepareSupport": True
            },
            "colorProvider": {
                "dynamicRegistration": True  # exceptional
            },
            "publishDiagnostics": {
                "relatedInformation": True
            },
            "selectionRange": {
                "dynamicRegistration": True
            }
        },
        "workspace": {
            "applyEdit": True,
            "didChangeConfiguration": {
                "dynamicRegistration": True
            },
            "executeCommand": {},
            "workspaceEdit": {
                "documentChanges": True,
                "failureHandling": "abort",
            },
            "workspaceFolders": True,
            "symbol": {
                "dynamicRegistration": True,  # exceptional
                "symbolKind": {
                    "valueSet": symbol_kinds
                }
            },
            "configuration": True
        },
        "window": {
            "workDoneProgress": True
        }
    }
    if config.experimental_capabilities is not None:
        capabilities['experimental'] = config.experimental_capabilities
    return {
        "processId": os.getpid(),
        "clientInfo": {
            "name": "Sublime Text LSP",
            "version": ".".join(map(str, __version__))
        },
        "rootUri": first_folder.uri() if first_folder else None,
        "rootPath": first_folder.path if first_folder else None,
        "workspaceFolders": [folder.to_lsp() for folder in workspace_folders] if workspace_folders else None,
        "capabilities": capabilities,
        "initializationOptions": sublime.expand_variables(config.init_options.get(), variables)
    }


# method -> (capability dotted path, optional registration dotted path)
# these are the EXCEPTIONS. The general rule is: method foo/bar --> (barProvider, barProvider.id)
METHOD_TO_CAPABILITY_EXCEPTIONS = {
    'workspace/symbol': ('workspaceSymbolProvider', None),
    'workspace/didChangeWorkspaceFolders': ('workspace.workspaceFolders',
                                            'workspace.workspaceFolders.changeNotifications'),
    'textDocument/didOpen': ('textDocumentSync.didOpen', None),
    'textDocument/didClose': ('textDocumentSync.didClose', None),
    'textDocument/didChange': ('textDocumentSync.change', None),
    'textDocument/didSave': ('textDocumentSync.save', None),
    'textDocument/willSave': ('textDocumentSync.willSave', None),
    'textDocument/willSaveWaitUntil': ('textDocumentSync.willSaveWaitUntil', None),
    'textDocument/formatting': ('documentFormattingProvider', None),
    'textDocument/documentColor': ('colorProvider', None)
}  # type: Dict[str, Tuple[str, Optional[str]]]


def method_to_capability(method: str) -> Tuple[str, str]:
    """
    Given a method, returns the corresponding capability path, and the associated path to stash the registration key.

    Examples:

        textDocument/definition --> (definitionProvider, definitionProvider.id)
        textDocument/references --> (referencesProvider, referencesProvider.id)
        textDocument/didOpen --> (textDocumentSync.openClose, textDocumentSync.openClose.id)
    """
    capability_path, registration_path = METHOD_TO_CAPABILITY_EXCEPTIONS.get(method, (None, None))
    if capability_path is None:
        capability_path = method.split('/')[1] + "Provider"
    if registration_path is None:
        # This path happens to coincide with the StaticRegistrationOptions' id, which is on purpose. As a consequence,
        # if a server made a "registration" via the initialize response, it can call client/unregisterCapability at
        # a later date, and the capability will pop from the capabilities dict.
        registration_path = capability_path + ".id"
    return capability_path, registration_path


class SessionViewProtocol(Protocol):

    session = None  # type: Session
    view = None  # type: sublime.View
    listener = None  # type: Any

    def on_capability_added_async(self, capability_path: str, options: Dict[str, Any]) -> None:
        ...

    def on_capability_removed_async(self, discarded_capabilities: DottedDict) -> None:
        ...

    def has_capability_async(self, capability_path: str) -> bool:
        ...

    def shutdown_async(self) -> None:
        ...

    def present_diagnostics_async(self, flags: int) -> None:
        ...


class SessionBufferProtocol(Protocol):

    session = None  # type: Session
    session_views = None  # type: WeakSet[SessionViewProtocol]
    file_name = None  # type: str

    def register_capability_async(self, registration_id: str, capability_path: str, options: Dict[str, Any]) -> None:
        ...

    def unregister_capability_async(self, registration_id: str) -> None:
        ...

    def on_diagnostics_async(self, diagnostics: List[Dict[str, Any]], version: Optional[int]) -> None:
        ...


class AbstractPlugin(metaclass=ABCMeta):
    """
    Inherit from this class to handle non-standard requests and notifications.
    Given a request/notification, replace the non-alphabetic characters with an underscore, and prepend it with "m_".
    This will be the name of your method.
    For instance, to implement the non-standard eslint/openDoc request, define the Python method

        def m_eslint_openDoc(self, params, request_id):
            session = self.weaksession()
            if session:
                webbrowser.open_tab(params['url'])
                session.send_response(Response(request_id, None))

    To handle the non-standard eslint/status notification, define the Python method

        def m_eslint_status(self, params):
            pass

    To understand how this works, see the __getattr__ method of the Session class.
    """

    @classmethod
    @abstractmethod
    def name(cls) -> str:
        """
        A human-friendly name. If your plugin is called "LSP-foobar", then this should return "foobar". If you also
        have your settings file called "LSP-foobar.sublime-settings", then you don't even need to re-implement the
        configuration method (see below).
        """
        raise NotImplementedError()

    @classmethod
    def configuration(cls) -> Tuple[sublime.Settings, str]:
        """
        Return the Settings object that defines the "command", "languages", and optionally the "initializationOptions",
        "default_settings", "env" and "tcp_port" as the first element in the tuple, and the path to the base settings
        filename as the second element in the tuple.

        The second element in the tuple is used to handle "settings" overrides from users properly. For example, if your
        plugin is called LSP-foobar, you would return "Packages/LSP-foobar/LSP-foobar.sublime-settings".

        The "command", "initializationOptions" and "env" are subject to template string substitution. The following
        template strings are recognized:

        $file
        $file_base_name
        $file_extension
        $file_name
        $file_path
        $platform
        $project
        $project_base_name
        $project_extension
        $project_name
        $project_path

        These are just the values from window.extract_variables(). Additionally,

        $cache_path   sublime.cache_path()
        $temp_dir     tempfile.gettempdir()
        $home         os.path.expanduser('~')
        $port         A random free TCP-port on localhost in case "tcp_port" is set to 0. This string template can only
                      be used in the "command"

        The "command" and "env" are expanded upon starting the subprocess of the Session. The "initializationOptions"
        are expanded upon doing the initialize request. "initializationOptions" does not expand $port.

        When you're managing your own server binary, you would typically place it in sublime.cache_path(). So your
        "command" should look like this: "command": ["$cache_path/LSP-foobar/server_binary", "--stdio"]
        """
        name = cls.name()
        basename = "LSP-{}.sublime-settings".format(name)
        filepath = "Packages/LSP-{}/{}".format(name, basename)
        return sublime.load_settings(basename), filepath

    @classmethod
    def additional_variables(cls) -> Optional[Dict[str, str]]:
        """
        In addition to the above variables, add more variables here to be expanded.
        """
        return None

    @classmethod
    def needs_update_or_installation(cls) -> bool:
        """
        If this plugin manages its own server binary, then this is the place to check whether the binary needs
        an update, or whether it needs to be installed before starting the language server.
        """
        return False

    @classmethod
    def install_or_update(cls) -> None:
        """
        Do the actual update/installation of the server binary. This runs in a separate thread, so don't spawn threads
        yourself here.
        """
        pass

    @classmethod
    def can_start(cls, window: sublime.Window, initiating_view: sublime.View,
                  workspace_folders: List[WorkspaceFolder], configuration: ClientConfig) -> Optional[str]:
        """
        Determines ability to start. This is called after needs_update_or_installation and after install_or_update.
        So you may assume that if you're managing your server binary, then it is already installed when this
        classmethod is called.

        :param      window:             The window
        :param      initiating_view:    The initiating view
        :param      workspace_folders:  The workspace folders
        :param      configuration:      The configuration

        :returns:   A string describing the reason why we should not start a language server session, or None if we
                    should go ahead and start a session.
        """
        return None

    def __init__(self, weaksession: 'weakref.ref[Session]') -> None:
        """
        Constructs a new instance.

        :param      weaksession:  A weak reference to the Session. You can grab a strong reference through
                                  self.weaksession(), but don't hold on to that reference.
        """
        self.weaksession = weaksession

    def on_workspace_configuration(cls, params: Dict, configuration: Any) -> None:
        """
        Override to augment configuration returned for the workspace/configuration request.

        :param      params:         A ConfigurationItem for which configuration is requested.
        :param      configuration:  The resolved configuration for given params.
        """
        pass


_plugins = {}  # type: Dict[str, Type[AbstractPlugin]]


def register_plugin(plugin: Type[AbstractPlugin]) -> None:
    global _plugins
    name = plugin.name()
    try:
        client_configs.add_external_config(name, *plugin.configuration())
        _plugins[name] = plugin
    except Exception as ex:
        exception_log('Failed to register plugin "{}"'.format(name), ex)


def unregister_plugin(plugin: Type[AbstractPlugin]) -> None:
    global _plugins
    name = plugin.name()
    try:
        client_configs.remove_external_config(name)
        _plugins.pop(name, None)
    except Exception as ex:
        exception_log('Failed to unregister plugin "{}"'.format(name), ex)
    finally:
        client_configs.update_configs()


def get_plugin(name: str) -> Optional[Type[AbstractPlugin]]:
    global _plugins
    return _plugins.get(name, None)


class _ViewFilter(metaclass=ABCMeta):

    __slots__ = ()

    @abstractmethod
    def matches(self, view: sublime.View) -> bool:
        pass

    def __call__(self, view: sublime.View) -> bool:
        return self.matches(view)


class _LanguageIdViewFilter(_ViewFilter):

    __slots__ = ("_language_id",)

    def __init__(self, language_id: str) -> None:
        self._language_id = language_id

    def matches(self, view: sublime.View) -> bool:
        return view.settings().get("lsp_language_id") == self._language_id


class _SublimeSelectorViewFilter(_ViewFilter):

    __slots__ = ("_selector",)

    def __init__(self, selector: str) -> None:
        self._selector = selector

    def matches(self, view: sublime.View) -> bool:
        return view.match_selector(0, self._selector) > 0


class _SchemeViewFilter(_ViewFilter):

    __slots__ = ("_scheme",)

    def __init__(self, scheme: str) -> None:
        self._scheme = scheme

    def matches(self, view: sublime.View) -> bool:
        debug("Don't know what to do with scheme", self._scheme)
        return True


class _PatternViewFilter(_ViewFilter):

    __slots__ = ("_pattern",)

    def __init__(self, pattern: str) -> None:
        self._pattern = pattern

    def matches(self, view: sublime.View) -> bool:
        file_name = view.file_name()
        if not file_name:
            return False
        debug("Need to use wcmatch library")
        return True


class ViewSelector:

    __slots__ = ("_filters",)

    def __init__(self, document_selector: List[Dict[str, Any]]) -> None:
        self._filters = []  # type: List[_ViewFilter]
        for document_filter in document_selector:
            language_id = document_filter.get("languageId")
            scheme = document_filter.get("scheme")
            pattern = document_filter.get("pattern")
            selector = document_filter.get("selector")  # Sublime extension
            if language_id:
                self._filters.append(_LanguageIdViewFilter(language_id))
            elif scheme:
                self._filters.append(_SchemeViewFilter(scheme))
            elif pattern:
                self._filters.append(_PatternViewFilter(pattern))
            elif selector:
                self._filters.append(_SublimeSelectorViewFilter(selector))
            else:
                debug("unknown document filter:", document_filter)

    def matches(self, view: sublime.View) -> bool:
        return any(filter(lambda f: f.matches(view), self._filters)) if self._filters else True

    def __call__(self, view: sublime.View) -> bool:
        return self.matches(view)


class _TextSyncPartBase:

    __slots__ = ("registration_id",)

    def __init__(self, registration_id: Optional[str]) -> None:
        self.registration_id = registration_id



class _DidOpenTextSync(_TextSyncPartBase):

    __slots__ = ()

    def __init__(self, registration_id: Optional[str]) -> None:
        super().__init__(registration_id)


class _ChangeTextSync(_TextSyncPartBase):

    __slots__ = ("sync_kind",)

    def __init__(self, registration_id: Optional[str], sync_kind: int) -> None:
        super().__init__(registration_id)
        self.sync_kind = sync_kind


class _WillSaveTextSync(_TextSyncPartBase):

    __slots__ = ()

    def __init__(self, registration_id: Optional[str]) -> None:
        super().__init__(registration_id)


class _WillSaveWaitUntilTextSync(_TextSyncPartBase):

    __slots__ = ()

    def __init__(self, registration_id: Optional[str]) -> None:
        super().__init__(registration_id)


class _DidSaveTextSync(_TextSyncPartBase):

    __slots__ = ("include_text",)

    def __init__(self, registration_id: Optional[str], include_text: bool) -> None:
        super().__init__(registration_id)
        self.include_text = include_text


class TextSync:

    __slots__ = ("did_open", "change", "will_save", "will_save_wait_until", "did_save", "did_close")

    def __init__(self) -> None:
        self.did_open = None  # type: Optional[_DidOpenTextSync]
        self.change = None  # type: Optional[_ChangeTextSync]
        self.will_save = None  # type: Optional[_WillSaveTextSync]
        self.will_save_wait_until = None  # type: Optional[_WillSaveWaitUntilTextSync]
        self.did_save = None  # type: Optional[_DidSaveTextSync]
        self.did_close = None  # type: Optional[DidCloseTextSync]

    @classmethod
    def from_legacy_integer(cls, change: int) -> "TextSync":
        result = cls()
        result.did_open = _DidOpenTextSync(None)
        result.change = _ChangeTextSync(None, change)
        result.did_close = DidCloseTextSync(None)
        return result

    @classmethod
    def from_dict(cls, textsync: Dict[str, Any]) -> "TextSync":
        result = cls()
        if "openClose" in textsync:
            result.did_open = _DidOpenTextSync(None)
            result.did_close = DidCloseTextSync(None)
        change_kind = textsync.get("change")
        if isinstance(change_kind, int):
            result.change = _ChangeTextSync(None, change_kind)
        if "willSave" in textsync:
            result.will_save = _WillSaveTextSync(None)
        if "willSaveWaitUntil" in textsync:
            result.will_save_wait_until = _WillSaveWaitUntilTextSync(None)
        save = textsync.get("save")
        if isinstance(save, bool):
            if save:
                result.did_save = _DidSaveTextSync(None, False)
        elif isinstance(save, dict):
            result.did_save = _DidSaveTextSync(None, bool(save.get("includeText", False)))
        return result

    def add_registration(
        self,
        registration_id: str,
        capability_path: str,
        options: Optional[Dict[str, Any]]
    ) -> None:
        tail = capability_path.split(".")[-1]
        if tail == "didOpen":
            self.did_open = _DidOpenTextSync(registration_id)
        elif tail == "change":
            self.change = _ChangeTextSync(registration_id, int(options.get("syncKind", 1)))
        elif tail == "willSave":
            self.will_save = _WillSaveTextSync(registration_id)
        elif tail == "willSaveWaitUntil":
            self.will_save_wait_until = _WillSaveWaitUntilTextSync(registration_id)
        elif tail == "didSave":
            self.did_save = _DidSaveTextSync(registration_id, bool(options.get("includeText", False)))
        elif tail == "didClose":
            self.did_close = DidCloseTextSync(registration_id)
        else:
            debug("unknown text sync capability:", capability_path)

    def discard_registration(self, registration_id: str) -> None:
        for slot in self.__slots__:
            attr = getattr(self, slot, None)
            if isinstance(attr, _TextSyncPartBase) and attr.registration_id == registration_id:
                setattr(self, slot, None)

    def should_notify_did_open(self) -> bool:
        return bool(self.did_open)

    def text_sync_kind(self) -> int:
        return self.change.sync_kind if self.change else TextDocumentSyncKindNone

    def should_notify_did_change(self) -> bool:
        return self.text_sync_kind() > TextDocumentSyncKindNone

    def should_notify_will_save(self) -> bool:
        return bool(self.will_save)

    def should_notify_did_save(self) -> Tuple[bool, bool]:
        if self.did_save:
            return True, self.did_save.include_text
        return False, False

    def should_notify_did_close(self) -> bool:
        return bool(self.did_close)


class _RegistrationData:

    __slots__ = ("registration_id", "capability_path", "options", "session_buffers", "selector")

    def __init__(self, registration_id: str, capability_path: str, options: Dict[str, Any]) -> None:
        self.registration_id = registration_id
        self.capability_path = capability_path
        document_selector = options.pop("documentSelector", None)
        if not isinstance(document_selector, list):
            document_selector = []
        self.selector = ViewSelector(document_selector)
        self.options = options
        self.session_buffers = WeakSet()  # type: WeakSet[SessionBufferProtocol]

    def __del__(self) -> None:
        for sb in self.session_buffers:
            sb.unregister_capability_async(self.registration_id)

    def check(self, sb: SessionBufferProtocol) -> None:
        for sv in sb.session_views:
            if self.selector.matches(sv.view):
                self.session_buffers.add(sb)
                sb.register_capability_async(self.registration_id, self.capability_path, self.options)
            return


class Session(Client):

    def __init__(self, manager: Manager, logger: Logger, workspace_folders: List[WorkspaceFolder],
                 config: ClientConfig, plugin_class: Optional[Type[AbstractPlugin]]) -> None:
        super().__init__(logger)
        self.config = config
        self.manager = weakref.ref(manager)
        self.window = manager.window()
        self.state = ClientStates.STARTING
        self.textsync = None  # type: Optional[TextSync]
        self._static_capabilities = DottedDict()
        self._dynamic_registrations = {}  # type: Dict[str, _RegistrationData]
        self.exiting = False
        self._init_callback = None  # type: Optional[InitCallback]
        self._exit_result = None  # type: Optional[Tuple[int, Optional[Exception]]]
        self._views_opened = 0
        self._workspace_folders = workspace_folders
        self._session_views = WeakSet()  # type: WeakSet[SessionViewProtocol]
        self._session_buffers = WeakSet()  # type: WeakSet[SessionBufferProtocol]
        self._progress = {}  # type: Dict[Any, Dict[str, str]]
        self._plugin_class = plugin_class
        self._plugin = None  # type: Optional[AbstractPlugin]

    def __del__(self) -> None:
        debug(self.config.command, "ended")

    def __getattr__(self, name: str) -> Any:
        """
        If we don't have a request/notification handler, look up the request/notification handler in the plugin.
        """
        if name.startswith('m_'):
            attr = getattr(self._plugin, name)
            if attr is not None:
                return attr
        raise AttributeError(name)

    # TODO: Create an assurance that the API doesn't change here as it can be used by plugins.
    def get_workspace_folders(self) -> List[WorkspaceFolder]:
        return self._workspace_folders

    # --- session view management --------------------------------------------------------------------------------------

    def register_session_view_async(self, sv: SessionViewProtocol) -> None:
        self._session_views.add(sv)
        self._views_opened += 1

    def unregister_session_view_async(self, sv: SessionViewProtocol) -> None:
        self._session_views.discard(sv)
        if not self._session_views:
            current_count = self._views_opened
            debounced(self.end_async, 3000, lambda: self._views_opened == current_count, async_thread=True)

    def session_views_async(self) -> Generator[SessionViewProtocol, None, None]:
        """
        It is only safe to iterate over this in the async thread
        """
        yield from self._session_views

    def session_view_for_view_async(self, view: sublime.View) -> Optional[SessionViewProtocol]:
        for sv in self.session_views_async():
            if sv.view == view:
                return sv
        return None

    # --- session buffer management ------------------------------------------------------------------------------------

    def register_session_buffer_async(self, sb: SessionBufferProtocol) -> None:
        self._session_buffers.add(sb)
        for data in self._dynamic_registrations.values():
            data.check(sb)

    def unregister_session_buffer_async(self, sb: SessionBufferProtocol) -> None:
        self._session_buffers.discard(sb)

    def session_buffers_async(self) -> Generator[SessionBufferProtocol, None, None]:
        """
        It is only safe to iterate over this in the async thread
        """
        yield from self._session_buffers

    def get_session_buffer_for_uri_async(self, uri: str) -> Optional[SessionBufferProtocol]:
        file_name = uri_to_filename(uri)
        for sb in self.session_buffers_async():
            try:
                if os.path.samefile(file_name, sb.file_name):
                    return sb
            except FileNotFoundError:
                pass
        return None

    # --- capability observers -----------------------------------------------------------------------------------------

    def can_handle(self, view: sublime.View, capability: Optional[str] = None) -> bool:
        file_name = view.file_name() or ''
        if self.config.match_view(view) and self.state == ClientStates.READY and self.handles_path(file_name):
            # If there's no capability requirement then this session can handle the view
            if capability is None:
                return True
            sv = self.session_view_for_view_async(view)
            return sv.has_capability_async(capability) if sv is not None else self.has_capability(capability)
        return False

    def has_capability(self, capability: str) -> bool:
        value = self.get_capability(capability)
        return value is not False and value is not None

    def get_capability(self, capability: str) -> Optional[Any]:
        return self._static_capabilities.get(capability)

    # --- misc methods -------------------------------------------------------------------------------------------------

    def handles_path(self, file_path: Optional[str]) -> bool:
        if self._supports_workspace_folders():
            # A workspace-aware language server handles any path, both inside and outside the workspaces.
            return True
        # If we end up here then the language server is workspace-unaware. This means there can be more than one
        # language server with the same config name. So we have to actually do the subpath checks.
        if not file_path:
            return False
        if not self._workspace_folders:
            return True
        for folder in self._workspace_folders:
            if is_subpath_of(file_path, folder.path):
                return True
        return False

    def update_folders(self, folders: List[WorkspaceFolder]) -> None:
        if self.should_notify_did_change_workspace_folders():
            added, removed = diff(self._workspace_folders, folders)
            params = {
                "event": {
                    "added": [a.to_lsp() for a in added],
                    "removed": [r.to_lsp() for r in removed]
                }
            }
            notification = Notification.didChangeWorkspaceFolders(params)
            self.send_notification(notification)
        if self._supports_workspace_folders():
            self._workspace_folders = folders

    def initialize(self, variables: Dict[str, str], transport: Transport, init_callback: InitCallback) -> None:
        self.transport = transport
        params = get_initialize_params(variables, self._workspace_folders, self.config)
        self._init_callback = init_callback
        self.send_request(Request.initialize(params), self._handle_initialize_success, self._handle_initialize_error)

    def _handle_initialize_success(self, result: Dict[str, Any]) -> None:
        textsync = result.pop("textDocumentSync", None)
        if isinstance(textsync, int):
            self.textsync = TextSync.from_legacy_integer(textsync)
        elif isinstance(textsync, dict):
            self.textsync = TextSync.from_dict(textsync)
        self._static_capabilities.assign(result.get('capabilities', dict()))
        if self._workspace_folders and not self._supports_workspace_folders():
            self._workspace_folders = self._workspace_folders[:1]
        self.state = ClientStates.READY
        if self._plugin_class is not None:
            self._plugin = self._plugin_class(weakref.ref(self))
        self.send_notification(Notification.initialized())
        self._maybe_send_did_change_configuration()
        execute_commands = self.get_capability('executeCommandProvider.commands')
        if execute_commands:
            debug("{}: Supported execute commands: {}".format(self.config.name, execute_commands))
        code_action_kinds = self.get_capability('codeActionProvider.codeActionKinds')
        if code_action_kinds:
            debug('{}: supported code action kinds: {}'.format(self.config.name, code_action_kinds))
        if self._init_callback:
            self._init_callback(self, False)
            self._init_callback = None

    def _handle_initialize_error(self, result: Any) -> None:
        self._exit_result = (result.get('code', -1), Exception(result.get('message', 'Error initializing server')))
        # Init callback called after transport is closed to avoid pre-mature GC of Session.
        self.end_async()

    def call_manager(self, method: str, *args: Any) -> None:
        mgr = self.manager()
        if mgr:
            getattr(mgr, method)(*args)

    def clear_diagnostics_async(self) -> None:
        # XXX: Remove this functionality?
        for sb in self.session_buffers_async():
            sb.on_diagnostics_async([], None)

    def on_stderr_message(self, message: str) -> None:
        self.call_manager('handle_stderr_log', self, message)
        self._logger.stderr_message(message)

    def _supports_workspace_folders(self) -> bool:
        return self.has_capability("workspace.workspaceFolders.supported")

    def _maybe_send_did_change_configuration(self) -> None:
        if self.config.settings:
            self.send_notification(did_change_configuration(self.config.settings, self._template_variables()))

    def _template_variables(self) -> Dict[str, str]:
        variables = extract_variables(self.window)
        if self._plugin_class is not None:
            extra_vars = self._plugin_class.additional_variables()
            if extra_vars:
                variables.update(extra_vars)
        return variables

    # --- server request handlers --------------------------------------------------------------------------------------

    def m_window_showMessageRequest(self, params: Any, request_id: Any) -> None:
        """handles the window/showMessageRequest request"""
        self.call_manager('handle_message_request', self, params, request_id)

    def m_window_showMessage(self, params: Any) -> None:
        """handles the window/showMessage notification"""
        self.call_manager('handle_show_message', self, params)

    def m_window_logMessage(self, params: Any) -> None:
        """handles the window/logMessage notification"""
        self.call_manager('handle_log_message', self, params)

    def m_workspace_workspaceFolders(self, _: Any, request_id: Any) -> None:
        """handles the workspace/workspaceFolders request"""
        self.send_response(Response(request_id, [wf.to_lsp() for wf in self._workspace_folders]))

    def m_workspace_configuration(self, params: Dict[str, Any], request_id: Any) -> None:
        """handles the workspace/configuration request"""
        items = []  # type: List[Any]
        requested_items = params.get("items") or []
        for requested_item in requested_items:
            configuration = self.config.settings.copy(requested_item.get('section') or None)
            if self._plugin:
                self._plugin.on_workspace_configuration(requested_item, configuration)
            items.append(configuration)
        self.send_response(Response(request_id, sublime.expand_variables(items, self._template_variables())))

    def m_workspace_applyEdit(self, params: Any, request_id: Any) -> None:
        """handles the workspace/applyEdit request"""
        edit = params.get('edit', {})
        self.window.run_command('lsp_apply_workspace_edit', {'changes': parse_workspace_edit(edit)})
        # TODO: We should ideally wait for all changes to have been applied. This is currently not "async".
        self.send_response(Response(request_id, {"applied": True}))

    def m_textDocument_publishDiagnostics(self, params: Any) -> None:
        """handles the textDocument/publishDiagnostics notification"""

        def run() -> None:
            uri = params["uri"]
            sb = self.get_session_buffer_for_uri_async(uri)
            if sb:
                sb.on_diagnostics_async(params["diagnostics"], params.get("version"))

        sublime.set_timeout_async(run)

    def m_client_registerCapability(self, params: Any, request_id: Any) -> None:
        """handles the client/registerCapability request"""

        def run() -> None:
            registrations = params["registrations"]
            for registration in registrations:
                registration_id = registration["id"]
                capability_path, _ = method_to_capability(registration["method"])
                debug("{}: registering capability:".format(self.config.name), capability_path)
                options = registration.get("registerOptions")  # type: Optional[Dict[str, Any]]
                if not isinstance(options, dict):
                    options = {}
                data = _RegistrationData(registration_id, capability_path, options)
                self._dynamic_registrations[registration_id] = data
                for sb in self.session_buffers_async():
                    data.check(sb)
            self.send_response(Response(request_id, None))

        sublime.set_timeout_async(run)

    def m_client_unregisterCapability(self, params: Any, request_id: Any) -> None:
        """handles the client/unregisterCapability request"""

        def run() -> None:
            unregistrations = params["unregisterations"]  # typo in the official specification
            for unregistration in unregistrations:
                data = self._dynamic_registrations.pop(unregistration["id"], None)
                if data is None:
                    continue
                debug("{}: unregistering capability:".format(self.config.name), data.capability_path)
            self.send_response(Response(request_id, None))

        sublime.set_timeout_async(run)

    def m_window_workDoneProgress_create(self, params: Any, request_id: Any) -> None:
        """handles the window/workDoneProgress/create request"""
        self._progress[params['token']] = dict()
        self.send_response(Response(request_id, None))

    def m___progress(self, params: Any) -> None:
        """handles the $/progress notification"""
        token = params['token']
        if token not in self._progress:
            debug('unknown $/progress token: {}'.format(token))
            return
        value = params['value']
        if value['kind'] == 'begin':
            self._progress[token]['title'] = value['title']  # mandatory
            self._progress[token]['message'] = value.get('message')  # optional
            self.window.status_message(self._progress_string(token, value))
        elif value['kind'] == 'report':
            self.window.status_message(self._progress_string(token, value))
        elif value['kind'] == 'end':
            if value.get('message'):
                status_msg = self._progress[token]['title'] + ': ' + value['message']
                self.window.status_message(status_msg)
            self._progress.pop(token, None)

    def _progress_string(self, token: Any, value: Dict[str, Any]) -> str:
        status_msg = self._progress[token]['title']
        progress_message = value.get('message')  # optional
        progress_percentage = value.get('percentage')  # optional
        if progress_message:
            self._progress[token]['message'] = progress_message
            status_msg += ': ' + progress_message
        elif self._progress[token]['message']:  # reuse last known message if not present
            status_msg += ': ' + self._progress[token]['message']
        if progress_percentage:
            fmt = ' ({:.1f}%)' if isinstance(progress_percentage, float) else ' ({}%)'
            status_msg += fmt.format(progress_percentage)
        return status_msg

    # --- shutdown dance -----------------------------------------------------------------------------------------------

    def exit(self) -> None:
        # exit() is only called when doing an explicit session shutdown. Ignore errors on exit then.
        self._exit_result = (0, None)
        super().exit()

    def end_async(self) -> None:
        # TODO: Ensure this function is called only from the async thread
        if self.exiting:
            return
        self.exiting = True
        self._plugin = None
        for sv in self.session_views_async():
            sv.shutdown_async()
        self._static_capabilities.clear()
        self._dynamic_registrations.clear()
        self.textsync = None
        self.state = ClientStates.STOPPING
        self.send_request(Request.shutdown(), self._handle_shutdown_result, self._handle_shutdown_result)

    def _handle_shutdown_result(self, _: Any) -> None:
        self.exit()

    def on_transport_close(self, exit_code: int, exception: Optional[Exception]) -> None:
        self.exiting = True
        self.state = ClientStates.STOPPING
        super().on_transport_close(exit_code, exception)
        self._response_handlers.clear()
        if self._exit_result:
            # Override potential exit error with a saved one.
            exit_code, exception = self._exit_result

        def run_async() -> None:
            mgr = self.manager()
            if mgr:
                if self._init_callback:
                    self._init_callback(self, True)
                    self._init_callback = None
                mgr.on_post_exit_async(self, exit_code, exception)

        sublime.set_timeout_async(run_async)
