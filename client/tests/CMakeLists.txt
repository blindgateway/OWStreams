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

set(STREAM_CLIENT_SOURCES
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/apiclient.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/apierror.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/credentialaccessmanager.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/credentialstore.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/sessioncontroller.cpp
)

# The model/controller suites additionally need the model layer, which pulls in
# Qt Quick's meta-type registration.
set(STREAM_MODEL_SOURCES
    ${STREAM_CLIENT_SOURCES}
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/librarymodel.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/homescreenmodel.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/appcontroller.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/playbackcontroller.cpp
)

foreach(suite credentialaccess apierror librarymodel appcontroller
             playbackcontroller sessioncontroller)
    if(suite STREQUAL "librarymodel" OR suite STREQUAL "appcontroller"
       OR suite STREQUAL "playbackcontroller")
        set(suite_sources ${STREAM_MODEL_SOURCES})
    else()
        set(suite_sources ${STREAM_CLIENT_SOURCES})
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
