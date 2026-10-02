cmake_minimum_required(VERSION 3.21)

project(StreamClient VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 6.5 REQUIRED COMPONENTS Core Gui Quick QuickControls2 Network Multimedia Svg)

qt_standard_project_setup(REQUIRES 6.5)

qt_add_executable(stream-client
    src/main.cpp
    src/apiclient.cpp
    src/apiclient.h
    src/apierror.cpp
    src/apierror.h
    src/credentialstore.cpp
    src/credentialstore.h
    src/sessioncontroller.cpp
    src/sessioncontroller.h
    src/librarymodel.cpp
    src/librarymodel.h
    src/homescreenmodel.cpp
    src/homescreenmodel.h
    src/appcontroller.cpp
    src/appcontroller.h
)

target_compile_definitions(stream-client PRIVATE
    QT_NO_CAST_FROM_ASCII
)

# Singletons: one instance, one source of truth.  Without this, every
# `Theme`/`Responsive` reference would construct a fresh QtObject and the
# tokens would not be shared.  The properties have to be set before the module
# is declared: qt_add_qml_module reads them while collecting the file list.
set_source_files_properties(qml/Theme.qml PROPERTIES QT_QML_SINGLETON_TYPE TRUE)
set_source_files_properties(qml/ui/Responsive.qml PROPERTIES QT_QML_SINGLETON_TYPE TRUE)
set_source_files_properties(qml/App.qml PROPERTIES QT_QML_SINGLETON_TYPE TRUE)

qt_add_qml_module(stream-client
    URI Stream
    VERSION 1.0
    QML_FILES
        qml/Main.qml
        qml/Theme.qml
        qml/App.qml
        qml/ui/Responsive.qml
        qml/ui/AppButton.qml
        qml/ui/AppTextField.qml
        qml/ui/PosterCard.qml
        qml/ui/PosterRail.qml
        qml/ui/StatusPane.qml
        qml/screens/ServerScreen.qml
        qml/screens/LoginScreen.qml
        qml/screens/ProfileScreen.qml
        qml/screens/HomeScreen.qml
)

target_include_directories(stream-client PRIVATE src)
target_link_libraries(stream-client PRIVATE
    Qt6::Core
    Qt6::Gui
    Qt6::Quick
    Qt6::QuickControls2
    Qt6::Network
    Qt6::Multimedia
    Qt6::Svg
)

# Tests are built only when asked for.  A client build on a packaging machine
# should not need the test sources to be present.
option(STREAM_BUILD_TESTS "Build the client test suite" OFF)
if(STREAM_BUILD_TESTS)
    enable_testing()
    add_subdirectory(tests)
endif()
