# Client tests.
#
# Deliberately *not* a QML test suite: the session state machine and the error
# taxonomy are plain C++, so testing them should not need a window, a platform
# plugin, or a display.  On a build machine with no display that is the
# difference between a suite that runs and one that hangs.
#
# Each test file is its own executable, because two QTEST_MAIN files in one
# binary is two definitions of main().
find_package(Qt6 6.5 REQUIRED COMPONENTS Core Network Gui Test Multimedia)

# The end-to-end suite needs Python (the server) and ffmpeg (a real clip), and
# it needs to know where the harness script lives.  `STREAM_BUILD_E2E` defaults
# to ON: a client test suite that silently skips itself is worse than useless,
# and the skip is reported loudly when the tools are missing.
option(STREAM_BUILD_E2E "Build the end-to-end suite (needs python3 and ffmpeg)" ON)

set(STREAM_CLIENT_SOURCES
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/apiclient.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/apierror.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/credentialaccessmanager.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/credentialstore.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/sessioncontroller.cpp
    # CaptureGuard is here rather than in the model set because `sessioncontroller`
    # is linked everywhere, and every suite that links it needs the moc-generated
    # `staticMetaObject` that `CaptureGuard`'s Q_OBJECT declares.
    #
    # The link failure this prevents is a confusing one: the moc output for a
    # Q_OBJECT class referenced from a *header* in the same target is generated
    # automatically, so nothing is missing at compile time, and the undefined
    # reference only appears at link time.
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/captureguard.cpp
)

# The fake server, shared by the suites that need a loopback HTTP endpoint that
# records what it was sent.  It lives here rather than being copied per suite:
# three copies of an HTTP parser is three copies of its bugs, and the "answer
# on the first readyRead" one already cost a debugging round.
set(STREAM_FAKE_SERVER ${CMAKE_CURRENT_SOURCE_DIR}/fakeserver.cpp
                       ${CMAKE_CURRENT_SOURCE_DIR}/fakeserver.h)

# The model/controller suites additionally need the model layer, which pulls in
# Qt Quick's meta-type registration.
set(STREAM_MODEL_SOURCES
    ${STREAM_CLIENT_SOURCES}
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/librarymodel.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/homescreenmodel.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/appcontroller.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/playbackcontroller.cpp
)

foreach(suite credentialaccess credentialstore apierror apiclient librarymodel
             appcontroller playbackcontroller sessioncontroller captureguard)
    if(suite STREQUAL "librarymodel" OR suite STREQUAL "appcontroller"
       OR suite STREQUAL "playbackcontroller")
        set(suite_sources ${STREAM_MODEL_SOURCES})
    else()
        set(suite_sources ${STREAM_CLIENT_SOURCES})
    endif()
    if(suite STREQUAL "apiclient")
        list(APPEND suite_sources ${STREAM_FAKE_SERVER})
    endif()
    qt_add_executable(stream-client-tests-${suite} tst_${suite}.cpp ${suite_sources})
    target_include_directories(stream-client-tests-${suite} PRIVATE ../src)
    # Qt6::Multimedia for every suite, not just the player one: the model set
    # includes playbackcontroller.cpp, whose include of <QAudioOutput> is a
    # compile-time dependency whether or not the suite calls into it.
    target_link_libraries(stream-client-tests-${suite} PRIVATE
        Qt6::Core Qt6::Network Qt6::Gui Qt6::Qml Qt6::Multimedia Qt6::Test)
    add_test(NAME client-${suite} COMMAND stream-client-tests-${suite})
    # Belt and braces: every suite here is QTEST_GUILESS, but a plugin on a
    # build machine must not be able to turn a pass into an abort.
    set_tests_properties(client-${suite} PROPERTIES ENVIRONMENT
        "QT_QPA_PLATFORM=offscreen")
endforeach()

if(STREAM_BUILD_E2E)
    # Not in the loop above: this suite needs a real `QCoreApplication` (the
    # network stack wants one), a real display-less environment, and two tools
    # that may not be installed.  A skip with a reason beats a failure that
    # means "ffmpeg is missing" twenty times over.
    qt_add_executable(stream-client-tests-e2e
        tst_e2e.cpp
        e2e/serverfixture.cpp
        e2e/serverfixture.h
        ${STREAM_CLIENT_SOURCES}
        ${STREAM_FAKE_SERVER}
        ${CMAKE_CURRENT_SOURCE_DIR}/../src/librarymodel.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/../src/homescreenmodel.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/../src/appcontroller.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/../src/playbackcontroller.cpp
    )
    target_include_directories(stream-client-tests-e2e PRIVATE ../src e2e)
    target_compile_definitions(stream-client-tests-e2e PRIVATE
        # An absolute path to harness.py, so the fixture does not have to
        # guess where the source tree is from its own location.
        STREAM_E2E_HARNESS="${CMAKE_CURRENT_SOURCE_DIR}/e2e/harness.py"
        QT_NO_CAST_FROM_ASCII
    )
    target_link_libraries(stream-client-tests-e2e PRIVATE
        Qt6::Core Qt6::Network Qt6::Gui Qt6::Qml Qt6::Multimedia Qt6::Test)
    # The build tree's copy of the QML module, in the library search path.
    #
    # Without it, running the binary directly works and running it under ctest
    # does not: `add_test` puts the test's *build* directory on the library
    # path but not the one below it, so the QML plugin fails to load and every
    # QML object is unregistered -- while the same binary, run from a shell
    # that happened to have the path set, loads it fine.  It showed up as
    # `TypeError: Cannot call method 'value' of undefined` in Main.qml, several
    # layers away from the cause.
    set(STREAM_MODULE_DIR ${CMAKE_CURRENT_BINARY_DIR}/../Stream)
    set_target_properties(stream-client-tests-e2e PROPERTIES
        BUILD_RPATH "${STREAM_MODULE_DIR}")
    add_test(NAME client-e2e COMMAND stream-client-tests-e2e)
    set_tests_properties(client-e2e PROPERTIES
        ENVIRONMENT "QT_QPA_PLATFORM=offscreen"
        # The server writes a database and runs ffprobe; give it room on a
        # loaded machine rather than failing a slow disk.
        TIMEOUT 600)
endif()
