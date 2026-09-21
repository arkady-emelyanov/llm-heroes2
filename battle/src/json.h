/***************************************************************************
 *   Standalone fheroes2 "battle only" build.                              *
 *                                                                         *
 *   Minimal JSON support. fheroes2 has no JSON dependency and this project *
 *   deliberately does not add one, so both directions are hand-rolled.     *
 *                                                                         *
 *   Writing is a streaming writer: the game only ever emits JSON, never    *
 *   inspects what it emitted, so there is no need to build a document      *
 *   tree. Reading produces a small immutable tree, which the scenario      *
 *   files need because they nest.                                         *
 ***************************************************************************/

#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace Json
{
    // Streaming JSON writer. Containers are opened and closed explicitly; the writer inserts
    // separators itself, so callers never deal with commas.
    class Writer
    {
    public:
        Writer & startObject( const char * key = nullptr )
        {
            return _open( key, '{' );
        }

        Writer & endObject()
        {
            return _close( '}' );
        }

        Writer & startArray( const char * key = nullptr )
        {
            return _open( key, '[' );
        }

        Writer & endArray()
        {
            return _close( ']' );
        }

        Writer & value( const char * key, const std::string & v )
        {
            _prefix( key );
            _quote( v );
            return *this;
        }

        Writer & value( const char * key, const char * v )
        {
            return value( key, std::string( v ) );
        }

        Writer & value( const char * key, const int64_t v )
        {
            _prefix( key );
            _out += std::to_string( v );
            return *this;
        }

        Writer & value( const char * key, const bool v )
        {
            _prefix( key );
            _out += v ? "true" : "false";
            return *this;
        }

        // Array elements: same as the keyed forms, without a key.
        Writer & value( const std::string & v )
        {
            return value( nullptr, v );
        }

        Writer & value( const int64_t v )
        {
            return value( nullptr, v );
        }

        const std::string & str() const
        {
            return _out;
        }

    private:
        std::string _out;
        bool _needComma{ false };

        Writer & _open( const char * key, const char open )
        {
            _prefix( key );
            _out += open;
            _needComma = false;
            return *this;
        }

        Writer & _close( const char close )
        {
            _out += close;
            _needComma = true;
            return *this;
        }

        void _prefix( const char * key )
        {
            if ( _needComma ) {
                _out += ',';
            }
            _needComma = true;

            if ( key != nullptr ) {
                _quote( key );
                _out += ':';
            }
        }

        void _quote( const std::string & v );
    };

    // A parsed JSON value. Accessors never throw: asking for the wrong type yields the fallback,
    // which keeps callers free of type checks they would only write to satisfy the compiler.
    class Value
    {
    public:
        enum class Type
        {
            Null,
            Boolean,
            Number,
            String,
            Array,
            Object
        };

        Type type() const
        {
            return _type;
        }

        bool isNull() const
        {
            return _type == Type::Null;
        }

        bool isNumber() const
        {
            return _type == Type::Number;
        }

        bool isString() const
        {
            return _type == Type::String;
        }

        bool isArray() const
        {
            return _type == Type::Array;
        }

        bool isObject() const
        {
            return _type == Type::Object;
        }

        bool asBool( const bool fallback = false ) const
        {
            return ( _type == Type::Boolean ) ? _boolean : fallback;
        }

        int64_t asInt( const int64_t fallback = 0 ) const
        {
            return ( _type == Type::Number ) ? _number : fallback;
        }

        // Also returns the original text of a number, so a caller can report what it was given.
        const std::string & asString() const
        {
            return _text;
        }

        // Elements of an array. Empty for anything else.
        const std::vector<Value> & items() const
        {
            return _items;
        }

        // Member of an object, or nullptr if absent or not an object.
        const Value * find( const std::string & key ) const;

        // The parser is the only thing that builds a Value, and it does so through Builder rather
        // than by reaching into these fields directly.
        friend class Builder;

    private:
        Type _type{ Type::Null };
        std::string _text;
        int64_t _number{ 0 };
        bool _boolean{ false };
        std::vector<Value> _items;
        std::map<std::string, Value> _members;
    };

    // Grants the parser write access to Value without opening its fields to everyone else.
    class Builder
    {
    public:
        static void setNull( Value & value )
        {
            value._type = Value::Type::Null;
        }

        static void setBoolean( Value & value, const bool boolean )
        {
            value._type = Value::Type::Boolean;
            value._boolean = boolean;
            value._text = boolean ? "true" : "false";
        }

        static void setNumber( Value & value, const int64_t number, std::string text )
        {
            value._type = Value::Type::Number;
            value._number = number;
            value._text = std::move( text );
        }

        static void setString( Value & value, std::string text )
        {
            value._type = Value::Type::String;
            value._text = std::move( text );
        }

        static void setArray( Value & value )
        {
            value._type = Value::Type::Array;
        }

        static void setObject( Value & value )
        {
            value._type = Value::Type::Object;
        }

        static void append( Value & value, Value element )
        {
            value._items.push_back( std::move( element ) );
        }

        static void insert( Value & value, const std::string & key, Value member )
        {
            value._members[key] = std::move( member );
        }

        static std::string & text( Value & value )
        {
            return value._text;
        }
    };

    // Parses a complete JSON document. Returns false and sets 'error' on malformed input; nesting
    // is limited so that a hostile or corrupt document cannot exhaust the stack.
    bool parse( const std::string & input, Value & out, std::string & error );
}
